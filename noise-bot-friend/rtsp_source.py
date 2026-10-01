import os
import time
import logging
import threading
import subprocess
from urllib.parse import urlsplit

SAMPLE_WIDTH = 2  # 16-bit PCM


def redact_url(url):
    """scheme://host:port/<redacted> -- RTSP paths/queries carry access tokens."""
    try:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.hostname}{':' + str(parts.port) if parts.port else ''}/<redacted>"
    except Exception:
        return "<redacted>"


class RTSPAudioSource:
    """
    Drop-in replacement for a PyAudio input stream.

    Decodes the first audio track of an RTSP stream with ffmpeg into 16-bit
    mono PCM and hands it out in fixed-size chunks. On a short read or ffmpeg
    exit it logs, backs off and reconnects; the missing samples are padded
    with silence (paced at real time) so the caller always gets a full chunk.
    """

    def __init__(self, url, rate=48000, io_timeout_seconds=5, max_backoff_seconds=30):
        if not url:
            raise ValueError("audio_source.url is empty")
        self.url = url
        self.rate = rate
        self.io_timeout_seconds = io_timeout_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.safe_url = redact_url(url)
        self.proc = None
        self.started_at = 0.0
        self.backoff = 1.0
        self.retry_at = 0.0

    def _start(self):
        cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-timeout", str(int(self.io_timeout_seconds * 1_000_000)),
            "-i", self.url,
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(self.rate),
            "-f", "s16le", "-",
        ]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.started_at = time.time()
        threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True).start()
        logging.info(f"RTSP audio: ffmpeg started for {self.safe_url} (pid {self.proc.pid})")

    def _drain_stderr(self, proc):
        # Keep the pipe from filling up; strip the token before logging.
        for line in proc.stderr:
            msg = line.decode("utf-8", "replace").strip().replace(self.url, self.safe_url)
            if msg:
                logging.warning(f"RTSP audio ffmpeg: {msg}")

    def _kill(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        try:
            proc.stdout.close()
        except Exception:
            pass

    def _fail(self, got, need):
        rc = self.proc.poll() if self.proc else None
        uptime = time.time() - self.started_at
        self._kill()
        if uptime > 60:
            self.backoff = 1.0
        logging.warning(
            f"RTSP audio: short read ({got}/{need} bytes, ffmpeg rc={rc}, up {uptime:.0f}s); "
            f"reconnecting in {self.backoff:.0f}s"
        )
        self.retry_at = time.time() + self.backoff
        self.backoff = min(self.backoff * 2, self.max_backoff_seconds)

    def read(self, n_frames, exception_on_overflow=False):
        need = n_frames * SAMPLE_WIDTH

        if self.proc is None:
            if time.time() < self.retry_at:
                time.sleep(n_frames / self.rate)
                return bytes(need)
            try:
                self._start()
            except Exception as e:
                logging.error(f"RTSP audio: failed to start ffmpeg: {e}")
                self.retry_at = time.time() + self.backoff
                self.backoff = min(self.backoff * 2, self.max_backoff_seconds)
                return bytes(need)

        buf = bytearray()
        fd = self.proc.stdout.fileno()
        while len(buf) < need:
            try:
                piece = os.read(fd, need - len(buf))
            except OSError:
                piece = b""
            if not piece:
                self._fail(len(buf), need)
                buf.extend(bytes(need - len(buf)))
                break
            buf.extend(piece)
        return bytes(buf)

    def stop_stream(self):
        self._kill()

    def close(self):
        self._kill()
