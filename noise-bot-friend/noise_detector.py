import os
import sys
import time
import json
import wave
import argparse
import logging
import threading
import urllib.request
import re
from collections import deque
from datetime import datetime, timezone, timedelta

# Force Mountain Time (America/Edmonton)
if hasattr(time, "tzset"):
    try:
        os.environ["TZ"] = "America/Edmonton"
        time.tzset()
    except Exception:
        pass

import numpy as np
import pyaudio

try:
    import scipy.signal as signal
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

from config_store import apply_config_patch, read_config
from notifier import Notifier
from audio_classifier import classify_audio

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "noise_bot.log")
LIVE_STATE_FILE = os.path.join(BASE_DIR, "live_audio_state.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
OUTPUT_DIR = os.path.join(BASE_DIR, "recordings")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)

FORMAT = pyaudio.paInt16
RATE = 48000
CHUNK = 4096

def get_a_weighting_coefficients(fs):
    if not HAS_SCIPY:
        return None, None
    f1 = 20.598997
    f2 = 107.65265
    f3 = 737.86223
    f4 = 12194.217
    A1000 = 1.9997

    NUMs = [(2 * np.pi * f4)**2 * (10**(A1000 / 20)), 0, 0, 0, 0]
    p1 = [1, 4 * np.pi * f1, (2 * np.pi * f1)**2]
    p2 = [1, 4 * np.pi * f4, (2 * np.pi * f4)**2]
    p3 = [1, 2 * np.pi * f2]
    p4 = [1, 2 * np.pi * f3]
    
    den = np.polymul(p1, p2)
    den = np.polymul(den, p3)
    den = np.polymul(den, p4)

    b, a = signal.bilinear(NUMs, den, fs)
    return b, a

def get_audio_devices():
    devs = []
    # 1. Read Linux ALSA kernel cards directly (never blocked by EBUSY)
    if os.path.exists("/proc/asound/cards"):
        try:
            with open("/proc/asound/cards", "r") as f:
                content = f.read()
            card_matches = re.findall(r"^\s*(\d+)\s*\[([^\]]+)\]:\s*([^\n-]+)(?:-\s*([^\n]+))?", content, re.MULTILINE)
            for m in card_matches:
                card_idx = int(m[0])
                card_name = (m[3].strip() if m[3] else m[2].strip())
                if "headphone" not in card_name.lower() and "vc4" not in card_name.lower():
                    devs.append({
                        "index": card_idx,
                        "name": f"{card_name} (hw:{card_idx},0)",
                        "channels": 2
                    })
        except Exception:
            pass

    # 2. PyAudio enumeration
    if not devs:
        p = pyaudio.PyAudio()
        try:
            info = p.get_host_api_info_by_index(0)
            numdevices = info.get('deviceCount', 0)
            for i in range(0, numdevices):
                try:
                    device_info = p.get_device_info_by_host_api_device_index(0, i)
                    if device_info.get('maxInputChannels', 0) > 0:
                        devs.append({
                            "index": i,
                            "name": device_info.get('name'),
                            "channels": device_info.get('maxInputChannels')
                        })
                except Exception:
                    pass
        finally:
            p.terminate()

    if not devs:
        devs.append({"index": 1, "name": "Amazon USB Streaming Mic (hw:1,0)", "channels": 2})

    return devs

def get_hardware_station_id(cfg=None):
    if cfg:
        f_id = (cfg.get("fleet_hub", {}).get("station_id") or "").strip()
        if f_id:
            return f_id if f_id.startswith("noise-bot-") else f"noise-bot-{f_id}"
        loc_name = (cfg.get("location_name") or "").strip()
        if loc_name and loc_name.lower() not in ["station", "balcony", "home", "balcony station", "community station", "noise-bot-subla"]:
            clean = re.sub(r'[^a-z0-9\-]', '', loc_name.lower().replace(" ", "-")).strip('-')
            if clean:
                return f"noise-bot-{clean}"
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if line.startswith("Serial"):
                    ser = line.split(":")[1].strip()
                    if ser and ser != "0000000000000000":
                        return f"noise-bot-{ser[-6:].lower()}"
    except Exception:
        pass
    try:
        import uuid
        mac = uuid.getnode()
        mac_hex = f"{mac:012x}"
        return f"noise-bot-{mac_hex[-6:]}"
    except Exception:
        pass
    return "noise-bot-station"

def upload_recording_to_hub(wav_path, event_meta):
    if not wav_path or not os.path.exists(wav_path):
        return
    try:
        import base64
        cfg = read_config(CONFIG_FILE)
        fleet_cfg = cfg.get("fleet_hub", {})
        if not fleet_cfg.get("enabled", True):
            return
        hub_url = fleet_cfg.get("hub_url", "").rstrip("/")
        if not hub_url or not hub_url.startswith("http"):
            return

        with open(wav_path, "rb") as wf:
            wav_bytes = wf.read()
        
        audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
        filename = os.path.basename(wav_path)
        station_id = get_hardware_station_id(cfg)
        
        payload = {
            "station_id": station_id,
            "filename": filename,
            "dba": float(event_meta.get("dba", 70.0)),
            "tailpipe_dba": float(event_meta.get("tailpipe_dba", 95.0)),
            "tag": event_meta.get("tag", "traffic"),
            "audio_b64": audio_b64,
            "timestamp": time.time()
        }
        
        endpoint = f"{hub_url}/api/recordings/upload"
        req_data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "EdmontonNoiseBot/1.4 (RaspberryPi; Linux aarch64)"
        }
        req = urllib.request.Request(endpoint, data=req_data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status in [200, 201]:
                logging.info(f"Uploaded audio {filename} to Central Hub ({len(wav_bytes)} bytes)")
                return True
        return False
    except Exception as e:
        logging.error(f"Failed to upload audio to Central Hub: {e}")
        return False

def sync_unuploaded_recordings():
    if not os.path.exists(OUTPUT_DIR):
        return
    try:
        cfg = read_config(CONFIG_FILE)
        fleet_cfg = cfg.get("fleet_hub", {})
        if not fleet_cfg.get("enabled", True):
            return
        hub_url = fleet_cfg.get("hub_url", "").rstrip("/")
        if not hub_url or not hub_url.startswith("http"):
            return

        sync_state_file = os.path.join(BASE_DIR, ".uploaded_recordings.json")
        uploaded = set()
        if os.path.exists(sync_state_file):
            try:
                with open(sync_state_file, "r") as f:
                    uploaded = set(json.load(f))
            except Exception:
                pass

        wav_files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.endswith(".wav") and not f.endswith("_temp.wav")])
        unuploaded = [f for f in wav_files if f not in uploaded]
        if not unuploaded:
            return

        new_uploaded = False
        # Take batch of newest first, plus some older if backlog exists
        batch = unuploaded[-10:]
        if len(unuploaded) > 10:
            batch = unuploaded[:5] + batch

        for f in batch:
            full_path = os.path.join(OUTPUT_DIR, f)
            match = re.search(r"noise_event_\d+_\d+_(\d+)dba_?([a-zA-Z0-9_-]*)\.wav", f)
            dba_val = float(match.group(1)) if match else 75.0
            tag_val = match.group(2) if match and match.group(2) else "traffic"
            dist = cfg.get("distance_to_road_meters") or 10.0
            loss = 20 * np.log10(max(0.5, dist) / 0.5)
            tailpipe = round(dba_val + loss, 1)

            ok = upload_recording_to_hub(full_path, {"dba": dba_val, "tailpipe_dba": tailpipe, "tag": tag_val})
            if ok:
                uploaded.add(f)
                new_uploaded = True
            time.sleep(0.3)

        if new_uploaded:
            try:
                with open(sync_state_file, "w") as f:
                    # Persist up to 25,000 uploaded filenames
                    json.dump(list(uploaded)[-25000:], f)
            except Exception:
                pass
    except Exception as e:
        logging.error(f"Error in recording sync loop: {e}")

def send_fleet_heartbeat(event_payload=None):
    if not os.path.exists(CONFIG_FILE):
        return
    try:
        cfg = read_config(CONFIG_FILE)
        fleet_cfg = cfg.get("fleet_hub", {})
        if not fleet_cfg.get("enabled", True):
            return
        hub_url = fleet_cfg.get("hub_url", "").rstrip("/")
        if not hub_url or not hub_url.startswith("http"):
            return

        live_dba = 0.0
        if os.path.exists(LIVE_STATE_FILE):
            with open(LIVE_STATE_FILE, "r", encoding="utf-8") as f:
                live_dba = json.load(f).get("current_dba", 0.0)

        total_events = 0
        if os.path.exists(OUTPUT_DIR):
            total_events = len([f for f in os.listdir(OUTPUT_DIR) if f.endswith(".wav")])

        station_id = get_hardware_station_id(cfg)

        payload = {
            "station_id": station_id,
            "station_name": fleet_cfg.get("station_name") or cfg.get("location_name", "Station"),
            "street_name": cfg.get("street_name", "Edmonton Corridor"),
            "location_name": cfg.get("location_name", "Balcony"),
            "floor_number": cfg.get("floor_number", 1),
            "horizontal_setback_meters": cfg.get("horizontal_setback_meters", 5.0),
            "distance_to_road_meters": cfg.get("distance_to_road_meters", 10.0),
            "current_dba": live_dba,
            "total_violations": total_events,
            "version": "v1.4"
        }

        payload.update({
            "config_reporting_version": 1,
            "config_update_ack": cfg.get("_hub_config_revision"),
            "config_state": {key: cfg[key] for key in (
                "floor_number", "horizontal_setback_meters", "distance_to_road_meters",
                "calibration_offset", "threshold_dba", "audio_device_index",
                "cooldown_period_minutes", "record_event_seconds", "save_audio_files") if key in cfg},
        })

        if event_payload:
            payload["event"] = event_payload

        endpoint = f"{hub_url}/api/heartbeat"
        req_data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "EdmontonNoiseBot/1.4 (RaspberryPi; Linux aarch64)"
        }
        req = urllib.request.Request(endpoint, data=req_data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                try:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    cfg_up = resp_data.get("config_update")
                    if cfg_up and isinstance(cfg_up, dict):
                        cur_cfg, changed = apply_config_patch(CONFIG_FILE, cfg_up, resp_data.get("config_update_revision"))
                        try:
                            if changed:
                                with open(os.path.join(BASE_DIR, ".reload_trigger"), "w") as rf:
                                    rf.write(str(time.time()))
                        except Exception:
                            pass
                        print(f"[Detector] Over-the-air config updated from Fleet Hub: floor={cur_cfg.get('floor_number')}, setback={cur_cfg.get('horizontal_setback_meters')}")
                except Exception:
                    pass
    except Exception:
        pass

def start_fleet_sync_thread():
    def sync_loop():
        sync_count = 0
        while True:
            time.sleep(3)
            send_fleet_heartbeat()
            sync_count += 1
            if sync_count % 20 == 0:
                sync_unuploaded_recordings()

    t = threading.Thread(target=sync_loop, daemon=True)
    t.start()

def main():
    parser = argparse.ArgumentParser(description="Noise Monitor Bot Engine")
    parser.add_argument("--list-devices", action="store_true", help="List audio input devices and exit")
    parser.add_argument("--device", type=int, default=None, help="Audio input device index")
    parser.add_argument("--test-mic", action="store_true", help="Live CLI meter mode")
    parser.add_argument("--threshold", type=float, default=None, help="Override noise threshold (dBA)")
    parser.add_argument("--offset", type=float, default=None, help="Override calibration offset (dB)")
    args = parser.parse_args()

    if args.list_devices:
        devs = get_audio_devices()
        print(json.dumps(devs, indent=2))
        return

    start_fleet_sync_thread()

    config = {}
    if os.path.exists(CONFIG_FILE):
        try:
            config = read_config(CONFIG_FILE)
        except Exception as e:
            logging.error(f"Failed to read {CONFIG_FILE}: {e}")

    threshold_dba = args.threshold or config.get("threshold_dba") or 70.0
    calibration_offset = args.offset or config.get("calibration_offset") or 95.0
    cooldown_period = (config.get("cooldown_period_minutes") or 2) * 60
    record_seconds = config.get("record_event_seconds") or 8
    save_audio = config.get("save_audio_files") if config.get("save_audio_files") is not None else True
    output_dir = config.get("output_directory") or OUTPUT_DIR
    device_index = args.device if args.device is not None else config.get("audio_device_index", None)

    notifier = Notifier(CONFIG_FILE)

    b, a = get_a_weighting_coefficients(RATE)
    filter_state = signal.lfilter_zi(b, a) * 0 if (HAS_SCIPY and b is not None) else None

    p = pyaudio.PyAudio()

    stream = None
    target_device = None
    channels = 1
    p_dev_count = p.get_device_count()

    # Priority 1: Specifically search PyAudio input devices for Yeti
    for i in range(p_dev_count):
        try:
            info = p.get_device_info_by_index(i)
            dev_name = (info.get("name") or "").lower()
            ch = int(info.get("maxInputChannels", 0))
            if ch > 0 and "yeti" in dev_name:
                target_device = i
                channels = min(2, max(1, ch))
                logging.info(f"Selected Yeti microphone: [{i}] {info.get('name')} ({channels} channels)")
                break
        except Exception:
            pass

    # Priority 2: If no Yeti, check explicit device_index from config
    if target_device is None and device_index is not None and str(device_index).lower() not in ["null", "none", "yeti"]:
        try:
            info = p.get_device_info_by_index(int(device_index))
            if info.get("maxInputChannels", 0) > 0:
                target_device = int(device_index)
                channels = min(2, max(1, int(info.get("maxInputChannels", 1))))
        except Exception:
            pass
        if target_device is None:
            hw_str = f"hw:{device_index},"
            for i in range(p_dev_count):
                try:
                    info = p.get_device_info_by_index(i)
                    if hw_str in (info.get("name") or "") and info.get("maxInputChannels", 0) > 0:
                        target_device = i
                        channels = min(2, max(1, int(info.get("maxInputChannels", 1))))
                        break
                except Exception:
                    pass

    # Priority 3: Fallback to any USB / mic device
    if target_device is None:
        for i in range(p_dev_count):
            try:
                info = p.get_device_info_by_index(i)
                dev_name = (info.get("name") or "").lower()
                ch = int(info.get("maxInputChannels", 0))
                if ch > 0 and any(k in dev_name for k in ["usb", "mic", "streaming"]):
                    target_device = i
                    channels = min(2, max(1, ch))
                    logging.info(f"Auto-selected microphone: [{i}] {info.get('name')} ({channels} channels)")
                    break
            except Exception:
                pass

    try:
        stream = p.open(
            format=FORMAT,
            channels=channels,
            rate=RATE,
            input=True,
            input_device_index=target_device,
            frames_per_buffer=CHUNK
        )
        logging.info(f"Opened audio input stream on Device {target_device} ({channels} channels, {RATE} Hz)")
    except Exception as e:
        logging.warning(f"Failed to open audio device {target_device} with {channels} ch: {e}. Retrying default...")
        try:
            channels = 1
            stream = p.open(
                format=FORMAT,
                channels=channels,
                rate=RATE,
                input=True,
                frames_per_buffer=CHUNK
            )
        except Exception as e2:
            logging.error(f"Critical audio initialization error: {e2}")
            p.terminate()
            return

    logging.info(f"Noise detector running. Threshold: {threshold_dba} dBA, Calibration: {calibration_offset} dB")

    pre_trigger_seconds = 2.0
    post_trigger_seconds = record_seconds - pre_trigger_seconds
    pre_trigger_chunks = int((RATE / CHUNK) * pre_trigger_seconds)
    post_trigger_chunks = int((RATE / CHUNK) * post_trigger_seconds)

    audio_history = deque(maxlen=pre_trigger_chunks)
    is_recording_event = False
    event_frames = []
    event_peak_dba = 0.0
    event_start_time = 0.0
    post_trigger_count = 0
    last_notification_time = 0.0
    last_state_write_time = 0.0
    last_config_check = 0.0
    config_mtime = os.stat(CONFIG_FILE).st_mtime_ns if os.path.exists(CONFIG_FILE) else None

    try:
        while True:
            try:
                data = stream.read(CHUNK, exception_on_overflow=False)
            except IOError as e:
                logging.warning(f"Audio stream buffer overflow: {e}")
                continue

            # Convert to float array and downmix stereo to mono if needed
            signal_data = np.frombuffer(data, dtype=np.int16).astype(np.float64)
            if channels > 1:
                signal_data = signal_data.reshape(-1, channels).mean(axis=1)

            audio_history.append(data)

            normalized = signal_data / 32768.0

            if HAS_SCIPY and b is not None and a is not None:
                weighted, filter_state = signal.lfilter(b, a, normalized, zi=filter_state)
            else:
                weighted = normalized

            rms = np.sqrt(np.mean(weighted ** 2))
            if rms > 1e-10:
                db_relative = 20 * np.log10(rms)
                dba = max(0.0, db_relative + calibration_offset)
            else:
                dba = 0.0

            now = time.time()
            if not is_recording_event and now - last_config_check > 1.0:
                last_config_check = now
                try:
                    current_mtime = os.stat(CONFIG_FILE).st_mtime_ns
                    if current_mtime != config_mtime:
                        config = read_config(CONFIG_FILE)
                        threshold_dba = args.threshold or config.get("threshold_dba") or 70.0
                        calibration_offset = args.offset if args.offset is not None else config.get("calibration_offset", 95.0)
                        config_mtime = current_mtime
                except (OSError, ValueError) as exc:
                    logging.warning("Settings reload deferred: %s", exc)
            if now - last_state_write_time > 0.2:
                last_state_write_time = now
                try:
                    state_payload = {
                        "timestamp": now,
                        "current_dba": round(dba, 1),
                        "is_recording": is_recording_event,
                        "threshold_dba": threshold_dba
                    }
                    with open(LIVE_STATE_FILE, "w", encoding="utf-8") as f:
                        json.dump(state_payload, f)
                        f.flush()
                except Exception:
                    pass

            if args.test_mic:
                bar_length = int(dba / 2)
                bar = "=" * bar_length + " " * (50 - bar_length)
                sys.stdout.write(f"\rCurrent level: {dba:5.1f} dB(A) | [{bar}]")
                sys.stdout.flush()
                continue

            current_time = time.time()
            in_cooldown = (current_time - last_notification_time) < cooldown_period

            if not is_recording_event:
                if dba >= threshold_dba and not in_cooldown:
                    logging.info(f"Threshold exceeded! {dba:.1f} dBA >= {threshold_dba} dBA. Recording event...")
                    is_recording_event = True
                    event_start_time = current_time
                    event_peak_dba = dba
                    event_frames = list(audio_history)
                    post_trigger_count = 0
            else:
                event_frames.append(data)
                post_trigger_count += 1
                if dba > event_peak_dba:
                    event_peak_dba = dba

                if post_trigger_count >= post_trigger_chunks:
                    is_recording_event = False
                    last_notification_time = current_time
                    wav_path = None
                    tag = "traffic"

                    if save_audio:
                        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                        temp_filename = f"noise_event_{timestamp}_{int(event_peak_dba)}dba_temp.wav"
                        temp_path = os.path.join(output_dir, temp_filename)

                        try:
                            os.makedirs(output_dir, exist_ok=True)
                            wf = wave.open(temp_path, 'wb')
                            wf.setnchannels(channels)
                            wf.setsampwidth(p.get_sample_size(FORMAT))
                            wf.setframerate(RATE)
                            wf.writeframes(b''.join(event_frames))
                            wf.close()

                            tag = classify_audio(temp_path)
                            final_filename = f"noise_event_{timestamp}_{int(event_peak_dba)}dba_{tag}.wav"
                            wav_path = os.path.join(output_dir, final_filename)
                            os.rename(temp_path, wav_path)
                            logging.info(f"Saved event recording ({tag}): {wav_path}")
                        except Exception as e:
                            logging.error(f"Failed to save WAV file: {e}")
                            wav_path = None

                    duration = post_trigger_seconds + pre_trigger_seconds
                    logging.info(f"Processing event: Peak {event_peak_dba:.1f} dBA, Duration {duration}s")
                    
                    # Run network notification in background thread so audio capture never blocks
                    threading.Thread(
                        target=notifier.notify,
                        args=(event_peak_dba,),
                        kwargs={"audio_file_path": wav_path, "wav_path": wav_path, "duration_seconds": duration},
                        daemon=True
                    ).start()

                    dist = config.get("distance_to_road_meters") or 10.0
                    loss = 20 * np.log10(max(0.5, dist) / 0.5)
                    tailpipe = round(event_peak_dba + loss, 1)
                    fn = final_filename if final_filename else f"noise_event_{timestamp}_{int(event_peak_dba)}dba_{tag}.wav"
                    
                    threading.Thread(
                        target=send_fleet_heartbeat,
                        kwargs={"event_payload": {"dba": round(event_peak_dba, 1), "tailpipe_dba": tailpipe, "tag": tag, "filename": fn}},
                        daemon=True
                    ).start()

                    if wav_path and os.path.exists(wav_path):
                        threading.Thread(
                            target=upload_recording_to_hub,
                            args=(wav_path, {"dba": round(event_peak_dba, 1), "tailpipe_dba": tailpipe, "tag": tag}),
                            daemon=True
                        ).start()

    except KeyboardInterrupt:
        logging.info("Stopping noise detector...")
    finally:
        if stream:
            stream.stop_stream()
            stream.close()
        p.terminate()

if __name__ == "__main__":
    main()
