#!/usr/bin/env python3
"""UDP audio source for the noisebot-mic ESP32 streamer (src/main.c).

Exposes the same interface the noise detector uses from PyAudio / the RTSP
source: read(n_frames, exception_on_overflow=False) -> bytes, stop_stream(),
close(). read() is always 48 kHz 16-bit mono (MIC1). When the board streams
stereo (protocol v3, "stream": "stereo"), read_stereo() returns MIC1/MIC2
interleaved; on a mono stream it returns MIC1 in both channels.

Gaps are filled with silence so the caller always gets a full chunk. Output is
paced by the ESP32's own sample clock (packet arrival), so there is no drift
against the host clock. During an outage, silence is paced by the host clock.

Run directly for a live level meter:
    python3 tools/udp_source.py --port 5005 --wav test.wav --seconds 30
"""

import argparse
import array
import logging
import math
import socket
import struct
import threading
import time
import wave

MAGIC = b"NB"
BASE_HEADER = struct.Struct("<2sBBI")  # magic, version, flags, seq
CAL_FIELD = struct.Struct("<h")         # v2: calibration offset, centi-dB (0 = none)
CHANNELS_FIELD = struct.Struct("<BB")   # v3: channels, reserved
HEADER_SIZE = {1: BASE_HEADER.size, 2: BASE_HEADER.size + CAL_FIELD.size,
               3: BASE_HEADER.size + CAL_FIELD.size + CHANNELS_FIELD.size}
SAMPLE_RATE = 48000
SAMPLE_WIDTH = 2
DEFAULT_FRAMES_PER_PACKET = 480

REORDER_WINDOW = 3     # packets: a missing seq is lost once this many newer ones arrived
MAX_GAP_FILL = 50      # packets: bigger mid-stream gaps resync instead of filling
OUTAGE_TIMEOUT = 1.0   # s without packets before emitting paced silence; Wi-Fi delivery can lag ~0.5 s in bursts
RESET_THRESHOLD = 1000 # packets: seq this far behind means the ESP32 rebooted
MAX_BUFFERED = 500     # packets held waiting for playout


class UdpAudioSource:
    def __init__(self, port=5005, bind="0.0.0.0", logger=None):
        self.log = logger or logging.getLogger(__name__)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.sock.bind((bind, port))
        self.sock.settimeout(0.5)

        self._cond = threading.Condition()
        self._packets = {}
        self._next_seq = None
        self._last_arrival = 0.0
        self._silence_next = None
        self._frames_per_packet = DEFAULT_FRAMES_PER_PACKET
        self._pending = b""
        self.channels = 1
        # dB to add to 20*log10(rms of A-weighted samples / 32768), as sent by
        # the ESP32 (protocol v2); None until a calibrated packet arrives
        self.calibration_offset = None
        self._running = True
        self.stats = {"received": 0, "lost": 0, "late": 0, "resyncs": 0,
                      "silence": 0, "bad": 0, "dropped": 0}

        self._thread = threading.Thread(target=self._rx_loop, name="udp-audio-rx", daemon=True)
        self._thread.start()
        self.log.info("Listening for ESP32 audio on UDP %s:%d", bind, port)

    def _rx_loop(self):
        while self._running:
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            if len(data) < BASE_HEADER.size:
                self.stats["bad"] += 1
                continue
            magic, version, _flags, seq = BASE_HEADER.unpack_from(data)
            header_size = HEADER_SIZE.get(version)
            payload_len = len(data) - (header_size or 0)
            if magic != MAGIC or header_size is None or payload_len <= 0 or payload_len % SAMPLE_WIDTH:
                self.stats["bad"] += 1
                continue
            cal = None
            if version >= 2:
                cal_cdb, = CAL_FIELD.unpack_from(data, BASE_HEADER.size)
                cal = cal_cdb / 100 if cal_cdb else None
            channels = 1
            if version >= 3:
                channels, _ = CHANNELS_FIELD.unpack_from(data, BASE_HEADER.size + CAL_FIELD.size)
                if channels not in (1, 2) or payload_len % (SAMPLE_WIDTH * channels):
                    self.stats["bad"] += 1
                    continue

            with self._cond:
                now = time.monotonic()
                after_outage = now - self._last_arrival > OUTAGE_TIMEOUT
                self._last_arrival = now
                if channels != self.channels:
                    # Mono <-> stereo switch: start clean in the new layout
                    self.log.info("Stream is now %s", "stereo" if channels == 2 else "mono")
                    self.channels = channels
                    self._packets.clear()
                    self._pending = b""
                    self._next_seq = None
                self._frames_per_packet = payload_len // (SAMPLE_WIDTH * channels)
                if cal != self.calibration_offset:
                    self.log.info("ESP32 reports its own calibration offset: %s dB (informational)", cal)
                    self.calibration_offset = cal

                if self._next_seq is None or after_outage:
                    if self._next_seq is not None:
                        self.log.info("Stream from %s resumed at seq %d", addr[0], seq)
                    else:
                        self.log.info("Stream from %s started at seq %d", addr[0], seq)
                    self._packets.clear()
                    self._next_seq = seq
                elif seq < self._next_seq:
                    if self._next_seq - seq > RESET_THRESHOLD:
                        self.log.warning("Seq jumped back %d -> %d, ESP32 restarted; resyncing",
                                         self._next_seq, seq)
                        self._packets.clear()
                        self._next_seq = seq
                        self.stats["resyncs"] += 1
                    else:
                        self.stats["late"] += 1
                        continue

                self._packets[seq] = data[header_size:]
                self.stats["received"] += 1
                while len(self._packets) > MAX_BUFFERED:
                    del self._packets[min(self._packets)]
                    self.stats["dropped"] += 1
                self._cond.notify()

    def _silence(self):
        return bytes(self._frames_per_packet * SAMPLE_WIDTH * self.channels)

    def _next_chunk(self):
        """One packet's worth of audio: the next packet in order, or silence."""
        with self._cond:
            while self._running:
                ns = self._next_seq
                if ns is not None and ns in self._packets:
                    self._next_seq += 1
                    if self._silence_next is not None:
                        self._silence_next = None
                    return self._packets.pop(ns)

                if self._packets:
                    lowest = min(self._packets)
                    if lowest - ns > MAX_GAP_FILL:
                        self.log.warning("Gap of %d packets, skipping ahead", lowest - ns)
                        self._next_seq = lowest
                        self.stats["resyncs"] += 1
                        continue
                    if max(self._packets) - ns >= REORDER_WINDOW:
                        self._next_seq += 1
                        self.stats["lost"] += 1
                        return self._silence()

                now = time.monotonic()
                since_rx = now - self._last_arrival
                if since_rx < OUTAGE_TIMEOUT:
                    self._cond.wait(OUTAGE_TIMEOUT - since_rx)
                    continue

                # Outage: keep the caller fed with silence at real-time pace
                if self._silence_next is None:
                    if self._last_arrival:
                        self.log.warning("No audio packets for %.2fs, emitting silence", since_rx)
                    else:
                        self.log.info("Waiting for ESP32 stream, emitting silence")
                    self._silence_next = now
                if now >= self._silence_next:
                    self._silence_next += self._frames_per_packet / SAMPLE_RATE
                    self.stats["silence"] += 1
                    return self._silence()
                self._cond.wait(self._silence_next - now)
            return self._silence()

    def _read_native(self, n_frames):
        """n_frames in the stream's own layout; returns (bytes, channels)."""
        while True:
            ch = self.channels
            need = n_frames * SAMPLE_WIDTH * ch
            buf = bytearray(self._pending)
            while len(buf) < need and self.channels == ch:
                buf += self._next_chunk()
            if self.channels != ch:
                continue  # layout changed mid-read; the rx thread reset the buffers
            self._pending = bytes(buf[need:])
            return bytes(buf[:need]), ch

    def read(self, n_frames, exception_on_overflow=False):
        data, ch = self._read_native(n_frames)
        if ch == 1:
            return data
        a = array.array("h", data)
        return a[0::2].tobytes()  # MIC1

    def read_stereo(self, n_frames):
        """Interleaved MIC1/MIC2; a mono stream is duplicated into both."""
        data, ch = self._read_native(n_frames)
        if ch == 2:
            return data
        a = array.array("h", data)
        out = array.array("h", bytes(len(data) * 2))
        out[0::2] = a
        out[1::2] = a
        return out.tobytes()

    def stop_stream(self):
        self._running = False
        with self._cond:
            self._cond.notify_all()

    def close(self):
        self.stop_stream()
        self.sock.close()
        self._thread.join(timeout=2)


def _dbfs(value):
    return 20 * math.log10(value / 32768) if value > 0 else -120.0


def main():
    parser = argparse.ArgumentParser(description="Live level meter for the ESP32 UDP mic")
    parser.add_argument("--port", type=int, default=5005)
    parser.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = forever)")
    parser.add_argument("--wav", help="also record to this WAV file")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    source = UdpAudioSource(port=args.port)
    wf = None
    if args.wav:
        wf = wave.open(args.wav, "wb")
        wf.setnchannels(1)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)

    chunk = SAMPLE_RATE // 10
    elapsed = 0.0
    prev = dict(source.stats)
    try:
        while not args.seconds or elapsed < args.seconds:
            sum_sq, peak = 0.0, 0
            for _ in range(10):
                data = source.read(chunk)
                if wf:
                    wf.writeframes(data)
                samples = array.array("h", data)
                sum_sq += sum(s * s for s in samples)
                peak = max(peak, max(samples), -min(samples))
            elapsed += 1.0
            rms = math.sqrt(sum_sq / (chunk * 10))
            delta = {k: source.stats[k] - prev[k] for k in source.stats}
            prev = dict(source.stats)
            print(f"{elapsed:6.0f}s  rms {_dbfs(rms):6.1f} dBFS  peak {_dbfs(peak):6.1f} dBFS  "
                  f"rx {delta['received']:3d}  lost {delta['lost']}  late {delta['late']}  "
                  f"silence {delta['silence']}  resync {delta['resyncs']}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        source.close()
        if wf:
            wf.close()


if __name__ == "__main__":
    main()
