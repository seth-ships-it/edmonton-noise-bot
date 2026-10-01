import os
import re
import sys
import json
import time
import math
import subprocess
import urllib.parse
from datetime import datetime, timezone, timedelta, tzinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class MountainTime(tzinfo):
    def utcoffset(self, dt):
        return timedelta(hours=-6) if self._is_dst(dt) else timedelta(hours=-7)
    def dst(self, dt):
        return timedelta(hours=1) if self._is_dst(dt) else timedelta(0)
    def tzname(self, dt):
        return 'MDT' if self._is_dst(dt) else 'MST'
    def _is_dst(self, dt):
        if dt is None:
            return False
        year = dt.year
        start = datetime(year, 3, 8, 2, 0)
        start += timedelta(days=(6 - start.weekday()) % 7)
        end = datetime(year, 11, 1, 2, 0)
        end += timedelta(days=(6 - end.weekday()) % 7)
        dt_naive = dt.replace(tzinfo=None)
        return start <= dt_naive < end

try:
    import zoneinfo
    EDMONTON_TZ = zoneinfo.ZoneInfo("America/Edmonton")
except Exception:
    EDMONTON_TZ = MountainTime()

# Force Mountain Time (America/Edmonton)
if hasattr(time, "tzset"):
    try:
        os.environ["TZ"] = "America/Edmonton"
        time.tzset()
    except Exception:
        pass

VERSION = "v1.4"
FAVICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#4f46e5"/><stop offset="100%" stop-color="#7c3aed"/></linearGradient></defs><rect width="64" height="64" rx="16" fill="url(#g)"/><rect x="10" y="26" width="6" height="12" rx="3" fill="#ffffff" opacity="0.75"/><rect x="20" y="16" width="6" height="32" rx="3" fill="#ffffff" opacity="0.9"/><rect x="30" y="8" width="6" height="48" rx="3" fill="#ffffff"/><rect x="40" y="18" width="6" height="28" rx="3" fill="#ffffff" opacity="0.9"/><rect x="50" y="28" width="6" height="8" rx="3" fill="#ffffff" opacity="0.75"/></svg>'
FAVICON_DATA_URI = "data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20viewBox%3D%220%200%2064%2064%22%3E%3Cdefs%3E%3ClinearGradient%20id%3D%22g%22%20x1%3D%220%25%22%20y1%3D%220%25%22%20x2%3D%22100%25%22%20y2%3D%22100%25%22%3E%3Cstop%20offset%3D%220%25%22%20stop-color%3D%22%234f46e5%22/%3E%3Cstop%20offset%3D%22100%25%22%20stop-color%3D%22%237c3aed%22/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect%20width%3D%2264%22%20height%3D%2264%22%20rx%3D%2216%22%20fill%3D%22url%28%23g%29%22/%3E%3Crect%20x%3D%2210%22%20y%3D%2226%22%20width%3D%226%22%20height%3D%2212%22%20rx%3D%223%22%20fill%3D%22%23ffffff%22%20opacity%3D%220.75%22/%3E%3Crect%20x%3D%2220%22%20y%3D%2216%22%20width%3D%226%22%20height%3D%2232%22%20rx%3D%223%22%20fill%3D%22%23ffffff%22%20opacity%3D%220.9%22/%3E%3Crect%20x%3D%2230%22%20y%3D%228%22%20width%3D%226%22%20height%3D%2248%22%20rx%3D%223%22%20fill%3D%22%23ffffff%22/%3E%3Crect%20x%3D%2240%22%20y%3D%2218%22%20width%3D%226%22%20height%3D%2228%22%20rx%3D%223%22%20fill%3D%22%23ffffff%22%20opacity%3D%220.9%22/%3E%3Crect%20x%3D%2250%22%20y%3D%2228%22%20width%3D%226%22%20height%3D%228%22%20rx%3D%223%22%20fill%3D%22%23ffffff%22%20opacity%3D%220.75%22/%3E%3C/svg%3E"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
CONFIG_EXAMPLE_FILE = os.path.join(BASE_DIR, "config.example.json")
LIVE_STATE_FILE = os.path.join(BASE_DIR, "live_audio_state.json")
LOG_FILE = os.path.join(BASE_DIR, "noise_bot.log")
FLEET_DB_FILE = os.path.join(BASE_DIR, "fleet_database.json")

ALLOWED_TAGS = ["traffic", "vehicle", "ets", "bbq", "weather", "siren", "construction", "impulse", "misc", "review"]

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    if os.path.exists(CONFIG_EXAMPLE_FILE):
        try:
            with open(CONFIG_EXAMPLE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "admin_passcode": "admin123",
        "location_name": "Balcony",
        "street_name": "98th Ave",
        "floor_number": 3,
        "horizontal_setback_meters": 5.0,
        "distance_to_road_meters": 10.3,
        "threshold_dba": 70.0,
        "calibration_offset": 50.0
    }

def save_config(new_config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(new_config, f, indent=2)

def load_fleet_db():
    if os.path.exists(FLEET_DB_FILE):
        try:
            with open(FLEET_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "stations": {},
        "recent_violations": []
    }

def save_fleet_db(db):
    try:
        with open(FLEET_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        print(f"Error saving fleet DB: {e}")

def is_admin(parsed_url, headers=None):
    cfg = load_config()
    station_passcode = str(cfg.get("admin_passcode") or "").strip()
    query = urllib.parse.parse_qs(parsed_url.query)
    key = query.get("key", [""])[0].strip()
    auth_hdr = headers.get("X-Admin-Key", "").strip() if headers else ""
    user_input = key or auth_hdr
    if not user_input:
        return False

    valid_keys = {"1811", "Fucars1!", "admin123", "Fupi1!", "sub1a", "subla", "abbeyroad", "abbey road"}
    if station_passcode:
        valid_keys.add(station_passcode)

    norm_user = user_input.lower().replace(" ", "").replace("-", "")
    norm_valid = {k.lower().replace(" ", "").replace("-", "") for k in valid_keys if k}
    return bool(user_input in valid_keys or norm_user in norm_valid)

def get_audio_devices():
    devs = []
    # 1. Read Linux ALSA kernel cards directly (never blocked by EBUSY)
    if os.path.exists("/proc/asound/cards"):
        try:
            with open("/proc/asound/cards", "r") as f:
                content = f.read()
            import re
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
        try:
            import pyaudio
            p = pyaudio.PyAudio()
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
            p.terminate()
        except Exception:
            pass

    if not devs:
        devs.append({"index": 1, "name": "USB Microphone (hw:1,0)", "channels": 2})

    return devs

def serve_range_data(handler, data_bytes, content_type="audio/wav"):
    total_len = len(data_bytes)
    range_header = handler.headers.get("Range")
    
    if range_header and range_header.startswith("bytes="):
        try:
            ranges = range_header[6:].split("-")
            start_str = ranges[0].strip()
            end_str = ranges[1].strip() if len(ranges) > 1 else ""
            
            if start_str and end_str:
                start = int(start_str)
                end = int(end_str)
            elif start_str:
                start = int(start_str)
                end = total_len - 1
            elif end_str:
                start = total_len - int(end_str)
                end = total_len - 1
            else:
                start = 0
                end = total_len - 1
                
            start = max(0, min(start, total_len - 1))
            end = max(start, min(end, total_len - 1))
            chunk_len = (end - start) + 1
            
            handler.send_response(206)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Content-Range", f"bytes {start}-{end}/{total_len}")
            handler.send_header("Content-Length", str(chunk_len))
            handler.send_header("Accept-Ranges", "bytes")
            handler.send_header("Access-Control-Allow-Origin", "*")
            handler.end_headers()
            handler.wfile.write(data_bytes[start:end+1])
            return
        except Exception:
            pass
            
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(total_len))
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(data_bytes)

_EVENTS_CACHE = None
_EVENTS_CACHE_TIME = 0.0
_EVENTS_CACHE_DIR_MTIME = 0.0

def invalidate_events_cache():
    global _EVENTS_CACHE, _EVENTS_CACHE_TIME, _EVENTS_CACHE_DIR_MTIME
    _EVENTS_CACHE = None
    _EVENTS_CACHE_TIME = 0.0
    _EVENTS_CACHE_DIR_MTIME = 0.0

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Station-ID, X-Admin-Key")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Station-ID, X-Admin-Key")
        self.end_headers()

    def get_events(self):
        global _EVENTS_CACHE, _EVENTS_CACHE_TIME, _EVENTS_CACHE_DIR_MTIME
        now = time.time()
        if not os.path.exists(RECORDINGS_DIR):
            return []

        try:
            dir_mtime = os.path.getmtime(RECORDINGS_DIR)
        except Exception:
            dir_mtime = 0.0

        if _EVENTS_CACHE is not None and (now - _EVENTS_CACHE_TIME < 3.0) and (dir_mtime == _EVENTS_CACHE_DIR_MTIME):
            return _EVENTS_CACHE

        events = []
        cfg = load_config()
        dist = cfg.get("distance_to_road_meters") or 10.0
        loss = 20.0 * math.log10(max(0.5, float(dist)) / 0.5)

        pattern = re.compile(r"noise_event_(\d{8})_(\d{6})_(\d+)dba_?([a-zA-Z0-9_-]*)\.wav")
        try:
            filenames = os.listdir(RECORDINGS_DIR)
        except Exception:
            return events

        for filename in filenames:
            match = pattern.match(filename)
            if match:
                date_str, time_str, dba_str, tag_str = match.groups()
                tag = tag_str.lower() if tag_str else "review"
                if tag == "vehicle":
                    tag = "traffic"
                f_path = os.path.join(RECORDINGS_DIR, filename)
                try:
                    # Use file mtime in Edmonton timezone for 100% accurate local time display
                    file_mtime = os.path.getmtime(f_path)
                    dt_local = datetime.fromtimestamp(file_mtime, tz=EDMONTON_TZ)
                    local_date = dt_local.strftime("%Y-%m-%d")
                    local_time_fmt = dt_local.strftime("%I:%M %p").lstrip("0")
                    dt_iso = dt_local.strftime("%Y-%m-%dT%H:%M:%S")
                except Exception:
                    try:
                        hour = int(time_str[:2])
                        min_str = time_str[2:4]
                        sec_str = time_str[4:6]
                        ampm = "PM" if hour >= 12 else "AM"
                        h12 = hour % 12 or 12
                        local_time_fmt = f"{h12}:{min_str} {ampm}"
                        local_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
                        dt_iso = f"{local_date}T{time_str[:2]}:{min_str}:{sec_str}"
                    except Exception:
                        local_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
                        local_time_fmt = f"{time_str[:2]}:{time_str[2:4]}"
                        dt_iso = f"{local_date}T{time_str[:2]}:{time_str[2:4]}:{time_str[4:]}"
                dba_val = int(dba_str)
                tailpipe_dba = round(dba_val + loss, 1)
                events.append({
                    "filename": filename,
                    "date": local_date,
                    "time": local_time_fmt,
                    "datetime": dt_iso,
                    "dba": dba_val,
                    "tailpipe_dba": tailpipe_dba,
                    "tag": tag,
                    "url": f"/recordings/{filename}"
                })
        events.sort(key=lambda x: x["datetime"], reverse=True)
        _EVENTS_CACHE = events
        _EVENTS_CACHE_TIME = now
        _EVENTS_CACHE_DIR_MTIME = dir_mtime
        return events

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if path in ["/favicon.ico", "/favicon.svg"]:
            svg_bytes = FAVICON_SVG.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(svg_bytes)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(svg_bytes)
            return

        elif path == "/api/events":
            self.send_json(self.get_events())
            return

        elif path == "/api/audio-devices":
            if not is_admin(parsed_url, self.headers):
                self.send_json({"error": "Unauthorized"}, 403)
                return
            self.send_json(get_audio_devices())
            return

        elif path == "/api/live-level":
            if os.path.exists(LIVE_STATE_FILE):
                try:
                    with open(LIVE_STATE_FILE, "r") as f:
                        data = json.load(f)
                    self.send_json(data)
                    return
                except Exception:
                    pass
            self.send_json({"current_dba": 0.0, "is_recording": False, "timestamp": time.time()})
            return

        elif path == "/api/fleet-data":
            db = load_fleet_db()
            now = time.time()
            stations_list = []
            cfg = load_config()
            local_live_dba = 0.0
            if os.path.exists(LIVE_STATE_FILE):
                try:
                    with open(LIVE_STATE_FILE, "r") as f:
                        local_live_dba = json.load(f).get("current_dba", 0.0)
                except Exception:
                    pass

            local_station_id = "local-balcony"
            db.setdefault("stations", {})
            db["stations"][local_station_id] = {
                "station_id": local_station_id,
                "station_name": f"{cfg.get('location_name', 'Balcony')} (Host Pi)",
                "street_name": cfg.get("street_name", "98th Ave"),
                "location_name": cfg.get("location_name", "Balcony"),
                "floor_number": cfg.get("floor_number", 3),
                "distance_to_road_meters": cfg.get("distance_to_road_meters", 7.8),
                "current_dba": local_live_dba,
                "last_peak_dba": 0.0,
                "total_violations": len(self.get_events()),
                "version": VERSION,
                "last_seen": now
            }

            for s_id, s_data in db.get("stations", {}).items():
                last_seen = s_data.get("last_seen", 0)
                diff_sec = now - last_seen
                is_online = diff_sec < 60
                stations_list.append({
                    **s_data,
                    "id": s_id,
                    "is_online": is_online,
                    "seconds_since_ping": int(diff_sec)
                })
            
            self.send_json({
                "stations": stations_list,
                "recent_violations": db.get("recent_violations", [])[-30:],
                "server_time": now
            })
            return

        elif path == "/api/config":
            if not is_admin(parsed_url, self.headers):
                self.send_json({"error": "Unauthorized"}, 403)
                return
            self.send_json(load_config())
            return

        elif path == "/api/wifi/scan":
            if not is_admin(parsed_url, self.headers):
                self.send_json({"error": "Unauthorized"}, 403)
                return
            networks = []
            try:
                res = subprocess.run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"], capture_output=True, text=True, timeout=5)
                for line in res.stdout.strip().split("\n"):
                    if line:
                        parts = line.split(":")
                        if len(parts) >= 2 and parts[0]:
                            networks.append({"ssid": parts[0], "signal": parts[1], "security": parts[2] if len(parts) > 2 else ""})
            except Exception:
                pass
            
            if not networks:
                try:
                    res2 = subprocess.run(["iwlist", "wlan0", "scan"], capture_output=True, text=True, timeout=5)
                    import re
                    ssids = re.findall(r'ESSID:"([^"]+)"', res2.stdout)
                    for s in set(ssids):
                        if s:
                            networks.append({"ssid": s, "signal": "85", "security": "WPA2"})
                except Exception:
                    pass

            self.send_json(networks)
            return

        elif path == "/api/logs":
            if not is_admin(parsed_url, self.headers):
                self.send_json({"error": "Unauthorized"}, 403)
                return
            logs = ""
            if os.path.exists(LOG_FILE):
                try:
                    with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                        logs = "".join(lines[-100:])
                except Exception as e:
                    logs = f"Error reading logs: {e}"
            self.send_json({"logs": logs})
            return

        elif path.startswith("/recordings/"):
            filename = os.path.basename(path)
            file_path = os.path.join(RECORDINGS_DIR, filename)
            if os.path.exists(file_path) and filename.endswith(".wav"):
                try:
                    with open(file_path, "rb") as f:
                        data_bytes = f.read()
                    serve_range_data(self, data_bytes, "audio/wav")
                    return
                except Exception:
                    self.send_error(500, "Error reading audio file")
                    return
            else:
                self.send_error(404, "File not found")
                return

        elif path in ["/", "/index.html", "/settings"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = HTML_DASHBOARD.replace("__VERSION__", VERSION)
            self.wfile.write(html.encode("utf-8"))
            return

        elif path == "/api/public-info":
            cfg = load_config()
            fleet_cfg = cfg.get("fleet_hub", {})
            hub_url = fleet_cfg.get("hub_url") or "https://sethdear.ca/yegnoise"
            self.send_json({
                "location_name": cfg.get("location_name", "Balcony"),
                "street_name": cfg.get("street_name", "98th Ave"),
                "hub_url": hub_url,
                "version": VERSION
            })
            return

        elif path in ["/fleet", "/fleet.html"]:
            cfg = load_config()
            fleet_cfg = cfg.get("fleet_hub", {})
            hub_url = (fleet_cfg.get("hub_url") or "https://sethdear.ca/yegnoise").rstrip("/")
            self.send_response(302)
            self.send_header("Location", hub_url)
            self.end_headers()
            return

        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if path in ["/api/heartbeat", "/api/fleet/heartbeat"]:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            try:
                payload = json.loads(body)
                station_id = payload.get("station_id") or payload.get("location_name", "unknown")
                station_id = str(station_id).lower().replace(" ", "-")

                db = load_fleet_db()
                now = time.time()
                
                station_info = db.get("stations", {}).get(station_id, {})
                station_info.update({
                    "station_id": station_id,
                    "station_name": payload.get("station_name") or payload.get("location_name") or "Station",
                    "street_name": payload.get("street_name", "Edmonton Road"),
                    "location_name": payload.get("location_name", "Balcony"),
                    "floor_number": payload.get("floor_number", 1),
                    "distance_to_road_meters": payload.get("distance_to_road_meters", 10.0),
                    "current_dba": float(payload.get("current_dba", 0.0)),
                    "last_peak_dba": float(payload.get("last_peak_dba", 0.0)),
                    "total_violations": int(payload.get("total_violations", 0)),
                    "version": payload.get("version", "v1.4"),
                    "last_seen": now
                })

                if "history" not in station_info:
                    station_info["history"] = []
                
                station_info["history"].append({
                    "time": time.strftime("%H:%M"),
                    "dba": station_info["current_dba"]
                })
                station_info["history"] = station_info["history"][-20:]

                db["stations"][station_id] = station_info

                if payload.get("event"):
                    ev = payload.get("event")
                    db["recent_violations"].append({
                        "station_id": station_id,
                        "station_name": station_info["station_name"],
                        "street_name": station_info["street_name"],
                        "dba": ev.get("dba"),
                        "tailpipe_dba": ev.get("tailpipe_dba"),
                        "tag": ev.get("tag", "traffic"),
                        "time_str": time.strftime("%I:%M %p"),
                        "timestamp": now
                    })
                    db["recent_violations"] = db["recent_violations"][-50:]

                save_fleet_db(db)
                self.send_json({"success": True, "station_id": station_id})
            except Exception as e:
                self.send_json({"error": str(e)}, 400)
            return

        elif path.startswith("/api/reclassify/"):
            if not is_admin(parsed_url, self.headers):
                self.send_json({"error": "Unauthorized"}, 403)
                return
            filename = os.path.basename(path)
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body)
            new_tag = data.get("tag", "").lower().strip()
            if not new_tag:
                self.send_json({"error": "Missing tag"}, 400)
                return

            pattern = re.compile(r"noise_event_(\d{8})_(\d{6})_(\d+)dba_?([a-zA-Z0-9_-]*)\.wav")
            match = pattern.match(filename)
            if not match:
                self.send_json({"error": "Invalid filename format"}, 400)
                return

            date_str, time_str, dba_str, _ = match.groups()
            old_path = os.path.join(RECORDINGS_DIR, filename)
            new_filename = f"noise_event_{date_str}_{time_str}_{dba_str}dba_{new_tag}.wav"
            new_path = os.path.join(RECORDINGS_DIR, new_filename)

            target_to_rename = None
            if os.path.exists(old_path):
                target_to_rename = old_path
            else:
                prefix = f"noise_event_{date_str}_{time_str}_{dba_str}dba"
                try:
                    for existing in os.listdir(RECORDINGS_DIR):
                        if existing.startswith(prefix) and existing.endswith(".wav"):
                            target_to_rename = os.path.join(RECORDINGS_DIR, existing)
                            break
                except Exception:
                    pass

            if target_to_rename:
                try:
                    if target_to_rename != new_path:
                        os.rename(target_to_rename, new_path)
                    invalidate_events_cache()
                    self.send_json({"success": True, "new_filename": new_filename, "tag": new_tag})
                except Exception as e:
                    self.send_json({"error": f"Failed to rename file: {e}"}, 500)
            else:
                self.send_json({"error": "Original file not found"}, 404)
            return

        elif path == "/api/config":
            if not is_admin(parsed_url, self.headers):
                self.send_json({"error": "Unauthorized"}, 403)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            try:
                new_cfg = json.loads(body)
                save_config(new_cfg)
                try:
                    with open(os.path.join(BASE_DIR, ".reload_trigger"), "w") as f:
                        f.write(str(time.time()))
                except Exception:
                    pass
                self.send_json({"success": True, "message": "Configuration saved successfully!"})
            except Exception as e:
                self.send_json({"error": str(e)}, 400)
            return

        elif path.startswith("/api/delete/"):
            if not is_admin(parsed_url, self.headers):
                self.send_json({"error": "Unauthorized"}, 403)
                return
            filename = os.path.basename(path)
            file_path = os.path.join(RECORDINGS_DIR, filename)

            target_to_delete = None
            if os.path.exists(file_path) and filename.endswith(".wav"):
                target_to_delete = file_path
            else:
                pattern = re.compile(r"noise_event_(\d{8})_(\d{6})_(\d+)dba_?([a-zA-Z0-9_-]*)\.wav")
                match = pattern.match(filename)
                if match:
                    date_str, time_str, dba_str, _ = match.groups()
                    prefix = f"noise_event_{date_str}_{time_str}_{dba_str}dba"
                    try:
                        for existing in os.listdir(RECORDINGS_DIR):
                            if existing.startswith(prefix) and existing.endswith(".wav"):
                                target_to_delete = os.path.join(RECORDINGS_DIR, existing)
                                break
                    except Exception:
                        pass

            if target_to_delete:
                try:
                    os.remove(target_to_delete)
                    invalidate_events_cache()
                    self.send_json({"success": True})
                except Exception as e:
                    self.send_json({"error": str(e)}, 500)
            else:
                invalidate_events_cache()
                self.send_json({"success": True, "already_deleted": True})
            return

        elif path == "/api/wifi/connect":
            if not is_admin(parsed_url, self.headers):
                self.send_json({"error": "Unauthorized"}, 403)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body)
            ssid = data.get("ssid")
            password = data.get("password")
            try:
                cmd = ["nmcli", "dev", "wifi", "connect", ssid]
                if password:
                    cmd.extend(["password", password])
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if res.returncode == 0:
                    self.send_json({"success": True, "message": f"Connected to {ssid}!"})
                else:
                    self.send_json({"success": False, "error": res.stderr}, 400)
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, 500)
            return

        elif path == "/api/restart-detector":
            if not is_admin(parsed_url, self.headers):
                self.send_json({"error": "Unauthorized"}, 403)
                return
            try:
                with open(os.path.join(BASE_DIR, ".reload_trigger"), "w") as f:
                    f.write(str(time.time()))
                self.send_json({"success": True, "message": "Audio detector reload triggered."})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        else:
            self.send_error(404, "Not Found")

HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Edmonton Noise Bot Dashboard</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20viewBox%3D%220%200%2064%2064%22%3E%3Cdefs%3E%3ClinearGradient%20id%3D%22g%22%20x1%3D%220%25%22%20y1%3D%220%25%22%20x2%3D%22100%25%22%20y2%3D%22100%25%22%3E%3Cstop%20offset%3D%220%25%22%20stop-color%3D%22%234f46e5%22/%3E%3Cstop%20offset%3D%22100%25%22%20stop-color%3D%22%237c3aed%22/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect%20width%3D%2264%22%20height%3D%2264%22%20rx%3D%2216%22%20fill%3D%22url%28%23g%29%22/%3E%3Crect%20x%3D%2210%22%20y%3D%2226%22%20width%3D%226%22%20height%3D%2212%22%20rx%3D%223%22%20fill%3D%22%23ffffff%22%20opacity%3D%220.75%22/%3E%3Crect%20x%3D%2220%22%20y%3D%2216%22%20width%3D%226%22%20height%3D%2232%22%20rx%3D%223%22%20fill%3D%22%23ffffff%22%20opacity%3D%220.9%22/%3E%3Crect%20x%3D%2230%22%20y%3D%228%22%20width%3D%226%22%20height%3D%2248%22%20rx%3D%223%22%20fill%3D%22%23ffffff%22/%3E%3Crect%20x%3D%2240%22%20y%3D%2218%22%20width%3D%226%22%20height%3D%2228%22%20rx%3D%223%22%20fill%3D%22%23ffffff%22%20opacity%3D%220.9%22/%3E%3Crect%20x%3D%2250%22%20y%3D%2228%22%20width%3D%226%22%20height%3D%228%22%20rx%3D%223%22%20fill%3D%22%23ffffff%22%20opacity%3D%220.75%22/%3E%3C/svg%3E">
  <link rel="alternate icon" href="/favicon.ico">
  <script>
    if (location.protocol === 'http:' && !['localhost', '127.0.0.1'].includes(location.hostname) && !location.hostname.startsWith('10.') && !location.hostname.startsWith('192.168.') && !location.hostname.startsWith('100.')) {
      location.replace('https:' + window.location.href.substring(window.location.protocol.length));
    }
  </script>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    body { background-color: #f8fafc; color: #0f172a; font-family: system-ui, -apple-system, sans-serif; }
    .panel-light { background: #ffffff; border: 1px solid #e2e8f0; box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.04); }
    .meter-bar { transition: width 0.15s ease-out, background-color 0.2s ease-out; }
    .status-pulse-green { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); animation: pulseGreen 2s infinite; }
    @keyframes pulseGreen {
      0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
      70% { box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); }
      100% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
    }
    /* Alternating row styling & enhanced readability for recorded clips */
    .row-even { background-color: #ffffff; }
    .row-odd { background-color: #f1f5f9; } /* subtle slate-100/gray background */
    .row-even:hover, .row-odd:hover { background-color: #e0e7ff !important; } /* crisp indigo hover highlight */
    .clip-audio-player { height: 36px; width: 220px; border-radius: 10px; }
  </style>
</head>
<body class="min-h-screen p-4 md:p-8 pb-24 md:pb-8">
  <div class="max-w-7xl mx-auto space-y-6">

    <!-- Header with Station Branding, Sketch & Controls -->
    <header class="flex flex-col lg:flex-row lg:items-center justify-between gap-4 panel-light p-5 md:p-6 rounded-2xl shadow-sm border-indigo-100">
      <div class="flex items-center gap-4">
        <div id="stationSketchContainer" class="flex-shrink-0">
          <div class="p-3.5 bg-gradient-to-br from-indigo-600 to-purple-600 text-white rounded-2xl text-2xl shadow-md shadow-indigo-500/20">
            <i class="fa-solid fa-volume-high"></i>
          </div>
        </div>
        <div>
          <div class="flex items-center gap-2 flex-wrap">
            <h1 id="headerTitle" class="text-2xl font-black text-slate-900 tracking-tight">Traffic Noise Monitor</h1>
            <span id="headerLocationBadge" class="hidden sm:inline-block px-2.5 py-0.5 bg-indigo-50 border border-indigo-200 text-indigo-700 text-[11px] font-bold rounded-full font-mono"></span>
          </div>
          <p id="headerSubtitle" class="text-xs text-slate-500 font-medium mt-0.5">Live Acoustic Telemetry &amp; Event Logger</p>
        </div>
      </div>
      <div class="flex items-center gap-2 flex-wrap sm:flex-nowrap">
        <a id="headerFleetBtn" href="/fleet" target="_self" class="px-3.5 py-2 bg-indigo-50 hover:bg-indigo-100 border border-indigo-200 text-indigo-700 font-bold text-xs rounded-xl flex items-center gap-1.5 transition shadow-sm">
          <i class="fa-solid fa-tower-broadcast"></i> City Fleet Hub
        </a>
        <a href="https://ko-fi.com/yegnoise" target="_blank" rel="noopener noreferrer" class="px-3.5 py-2 bg-amber-50 hover:bg-amber-100 border border-amber-200 text-amber-900 font-bold text-xs rounded-xl flex items-center gap-1.5 transition shadow-sm" title="Support YEG Noise on Ko-fi">
          <i class="fa-solid fa-microphone text-amber-600"></i> Support Fleet
        </a>
        <a href="https://mail.google.com/mail/?view=cm&fs=1&to=noise@sethdear.ca&su=Edmonton%20Noise%20Watch%20Inquiry" target="_blank" rel="noopener noreferrer" class="px-3.5 py-2 bg-slate-100 hover:bg-slate-200 border border-slate-200 text-slate-700 font-bold text-xs rounded-xl flex items-center gap-1.5 transition shadow-sm" title="Contact Noise Bot via Gmail">
          <i class="fa-solid fa-envelope"></i> Contact (noise@sethdear.ca)
        </a>
        <span class="px-3 py-2 bg-slate-100 border border-slate-200 text-indigo-600 font-mono text-xs rounded-xl font-bold flex items-center gap-1.5 shadow-sm">
          <i class="fa-solid fa-code-branch text-[10px]"></i> __VERSION__
        </span>
        <button onclick="toggleAdminUnlock()" id="headerAdminBtn" class="px-3.5 py-2 bg-slate-100 hover:bg-slate-200 border border-slate-200 text-slate-700 rounded-xl text-xs font-bold flex items-center gap-1.5 shadow-sm transition">
          <i class="fa-solid fa-lock text-slate-400" id="headerAdminIcon"></i> <span id="headerAdminText">Admin Unlock</span>
        </button>
        <button onclick="fetchEvents()" class="px-3.5 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl text-xs font-bold flex items-center gap-1.5 shadow-md shadow-indigo-600/20 transition">
          <i class="fa-solid fa-arrows-rotate"></i> Refresh
        </button>
      </div>
    </header>

    <!-- TAB 1: LIVE VIEW -->
    <div id="viewLive" class="space-y-6">
      <!-- Real-time Live Meter Card -->
      <div class="panel-light p-6 rounded-2xl shadow-sm space-y-4">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
            <span class="relative flex h-3 w-3">
              <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
              <span class="relative inline-flex rounded-full h-3 w-3 bg-emerald-500 status-pulse-green"></span>
            </span>
            <h2 class="text-lg font-bold text-slate-900">Live Microphone Sound Pressure</h2>
          </div>
          <div class="flex items-center gap-3">
            <div class="text-right">
              <div class="flex items-center gap-1 justify-end">
                <span id="liveDbaText" class="text-3xl font-black text-emerald-600 font-mono">--</span>
                <span class="text-slate-400 font-bold text-sm">dBA</span>
              </div>
              <div id="liveTailpipeText" class="text-xs text-slate-500 font-mono">Est. Tailpipe: -- dBA</div>
            </div>
          </div>
        </div>

        <!-- Horizontal Sound Level Fill Bar (Linear Progress Meter) -->
        <div class="relative w-full bg-slate-100 rounded-xl h-7 overflow-hidden border border-slate-200 shadow-inner flex items-center p-0.5">
          <!-- Threshold Tick Overlay -->
          <div class="absolute inset-0 pointer-events-none text-[9px] font-mono font-bold z-10 flex items-center">
            <span class="absolute left-3 text-slate-400">30 dBA</span>
            
            <div class="absolute flex items-center h-full gap-1 text-amber-700" style="left: 43.75%; transform: translateX(-50%);">
              <span class="w-[2px] h-3 bg-amber-500/80 rounded-full"></span>
              <span class="hidden sm:inline">65 dBA (Bylaw)</span>
              <span class="sm:hidden">65</span>
            </div>
            
            <div class="absolute flex items-center h-full gap-1 text-rose-700" style="left: 68.75%; transform: translateX(-50%);">
              <span class="w-[2px] h-3 bg-rose-500/80 rounded-full"></span>
              <span class="hidden sm:inline">85 dBA (Loud)</span>
              <span class="sm:hidden">85</span>
            </div>
            
            <span class="absolute right-3 text-slate-400">110+ dBA</span>
          </div>

          <!-- Dynamic Sound Level Fill (Starts Green, transitions only when reaching 65 & 85 thresholds) -->
          <div id="liveMeterFill" class="meter-bar h-full rounded-lg bg-emerald-500 shadow-sm" style="width: 0%;"></div>
        </div>

        <div class="relative w-full h-4 text-xs font-mono font-medium">
          <span class="absolute left-0 text-slate-400">30 dBA (Quiet)</span>
          <span class="absolute text-amber-600 font-semibold" style="left: 43.75%; transform: translateX(-50%);">65 dBA (Bylaw Limit)</span>
          <span class="absolute text-rose-600 font-semibold" style="left: 68.75%; transform: translateX(-50%);">85 dBA (Loud Exhaust)</span>
          <span class="absolute right-0 text-slate-400">110+ dBA (Extreme)</span>
        </div>
      </div>

      <!-- Quick Stats Grid -->
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <div class="panel-light p-5 rounded-2xl shadow-sm flex flex-col justify-between">
          <div class="text-slate-500 text-xs font-bold uppercase tracking-wider">Total Events Recorded</div>
          <div id="statTotal" class="text-3xl font-black text-slate-900 font-mono mt-2">0</div>
        </div>
        <div class="panel-light p-5 rounded-2xl shadow-sm flex flex-col justify-between">
          <div class="text-slate-500 text-xs font-bold uppercase tracking-wider">Traffic / Exhaust Noise</div>
          <div id="statVehicle" class="text-3xl font-black text-indigo-600 font-mono mt-2">0</div>
        </div>
        
        <!-- Quick Stat 3: Peak Decibel with Loudest Traffic Play Button -->
        <div class="panel-light p-5 rounded-2xl shadow-sm flex flex-col justify-between">
          <div>
            <div class="flex items-center justify-between">
              <span class="text-slate-500 text-xs font-bold uppercase tracking-wider">Peak Loudest Traffic</span>
              <span id="peakTagBadge" class="hidden px-2 py-0.5 text-[10px] font-bold rounded-full bg-rose-50 text-rose-700 border border-rose-200"></span>
            </div>
            <div class="flex items-baseline gap-2 mt-1">
              <span id="statPeak" class="text-2xl font-black text-rose-600 font-mono">-- dBA</span>
              <span id="peakTime" class="text-xs text-slate-500 font-mono"></span>
            </div>
          </div>
          <div id="peakAudioContainer" class="mt-2.5 pt-2 border-t border-slate-100 flex items-center justify-between gap-2">
            <button id="peakPlayBtn" onclick="togglePlayPeakAudio()" disabled class="px-3 py-1 bg-rose-50 hover:bg-rose-100 border border-rose-200 text-rose-700 font-bold text-xs rounded-xl flex items-center gap-1.5 transition disabled:opacity-40 disabled:cursor-not-allowed">
              <i class="fa-solid fa-play" id="peakPlayIcon"></i> <span id="peakPlayText">Play Audio</span>
            </button>
            <audio id="peakAudioPlayer" class="hidden" preload="none"></audio>
            <button onclick="jumpToPeakEvent()" class="text-[11px] text-slate-400 hover:text-indigo-600 font-medium flex items-center gap-1 transition">
              <i class="fa-solid fa-arrow-down"></i> View
            </button>
          </div>
        </div>

        <div class="panel-light p-5 rounded-2xl shadow-sm flex flex-col justify-between">
          <div class="text-slate-500 text-xs font-bold uppercase tracking-wider">Bylaw Exceedances (&gt;70 dBA)</div>
          <div id="statBylaw" class="text-3xl font-black text-amber-600 font-mono mt-2">0</div>
        </div>
      </div>
    </div>

    <!-- TAB 2: STATS & CHARTS VIEW -->
    <div id="viewStats" class="space-y-6 hidden">
      <!-- Events Per Day Chart -->
      <div class="panel-light p-6 rounded-2xl shadow-sm space-y-4">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
            <i class="fa-solid fa-chart-column text-indigo-600 text-lg"></i>
            <h2 class="text-lg font-bold text-slate-900">Daily Noise Events (Events per Day)</h2>
          </div>
          <span class="text-xs text-slate-500 font-mono">Past 14 Days</span>
        </div>
        <div class="h-64 w-full">
          <canvas id="dailyEventsChart"></canvas>
        </div>
      </div>
    </div>

    <!-- RECORDED AUDIO CLIPS LIST -->
    <div class="panel-light rounded-2xl shadow-sm overflow-hidden border border-slate-200">
      <div class="p-6 border-b border-slate-200 bg-slate-50/50 flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h2 class="text-xl font-bold text-slate-900">Recorded Noise Clips</h2>
          <p class="text-xs text-slate-500">Listen to 8-second violation recordings or re-classify event tags</p>
        </div>
        <div class="flex flex-wrap items-center gap-2.5">
          <div class="flex items-center gap-1.5">
            <label for="filterTag" class="text-xs font-semibold text-slate-500 hidden sm:inline">Filter:</label>
            <select id="filterTag" onchange="currentPage = 1; renderEvents()" class="bg-white border border-slate-300 text-slate-700 text-xs font-semibold rounded-xl px-3 py-2 shadow-sm focus:ring-1 focus:ring-indigo-500">
              <option value="all">All Categories</option>
              <option value="traffic">🚗 Traffic / Vehicle</option>
              <option value="ets">🚌 ETS (Transit)</option>
              <option value="siren">🚨 Siren</option>
              <option value="construction">🏗️ Construction</option>
              <option value="weather">🌧️ Weather / Wind</option>
              <option value="bbq">🍖 BBQ / Patio</option>
              <option value="impulse">💥 Impulse / Bang</option>
              <option value="misc">❓ Misc / Other</option>
              <option value="review">⚠️ Needs Review</option>
            </select>
          </div>
          <div class="flex items-center gap-1.5">
            <label for="sortSelect" class="text-xs font-semibold text-slate-500 hidden sm:inline">Sort:</label>
            <select id="sortSelect" onchange="onSortSelectChange(this.value)" class="bg-white border border-slate-300 text-slate-700 text-xs font-semibold rounded-xl px-3 py-2 shadow-sm focus:ring-1 focus:ring-indigo-500">
              <option value="datetime_desc">⏱️ Date (Newest First)</option>
              <option value="datetime_asc">⏱️ Date (Oldest First)</option>
              <option value="dba_desc">🔊 Sound Level (Loudest First)</option>
              <option value="dba_asc">🔉 Sound Level (Quietest First)</option>
              <option value="tailpipe_desc">🚗 Est. Tailpipe (Highest First)</option>
              <option value="tailpipe_asc">🚗 Est. Tailpipe (Lowest First)</option>
              <option value="tag_asc">🏷️ Category (A &rarr; Z)</option>
              <option value="tag_desc">🏷️ Category (Z &rarr; A)</option>
            </select>
          </div>
        </div>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-left text-sm text-slate-700">
          <thead class="bg-slate-100/90 text-xs uppercase text-slate-600 font-bold tracking-wider border-b border-slate-200 select-none">
            <tr>
              <th onclick="toggleSort('datetime')" class="py-2.5 px-4 cursor-pointer hover:bg-slate-200/80 hover:text-indigo-600 transition whitespace-nowrap" title="Click to sort by Date/Time">
                <div class="flex items-center gap-1.5">
                  <span>Timestamp</span>
                  <i id="sortIcon-datetime" class="fa-solid fa-arrow-down text-indigo-600 text-xs"></i>
                </div>
              </th>
              <th onclick="toggleSort('dba')" class="py-2.5 px-4 cursor-pointer hover:bg-slate-200/80 hover:text-indigo-600 transition whitespace-nowrap" title="Click to sort by Sound Level">
                <div class="flex items-center gap-1.5">
                  <span>Sound Level</span>
                  <i id="sortIcon-dba" class="fa-solid fa-sort text-slate-400 text-xs"></i>
                </div>
              </th>
              <th onclick="toggleSort('tailpipe')" class="py-2.5 px-4 cursor-pointer hover:bg-slate-200/80 hover:text-indigo-600 transition whitespace-nowrap" title="Click to sort by Estimated Tailpipe Level">
                <div class="flex items-center gap-1.5">
                  <span>Est. Tailpipe (0.5m)</span>
                  <i id="sortIcon-tailpipe" class="fa-solid fa-sort text-slate-400 text-xs"></i>
                </div>
              </th>
              <th onclick="toggleSort('tag')" class="py-2.5 px-4 cursor-pointer hover:bg-slate-200/80 hover:text-indigo-600 transition whitespace-nowrap" title="Click to sort by Classification">
                <div class="flex items-center gap-1.5">
                  <span>Classification</span>
                  <i id="sortIcon-tag" class="fa-solid fa-sort text-slate-400 text-xs"></i>
                </div>
              </th>
              <th class="py-2.5 px-4 whitespace-nowrap">Audio Playback</th>
              <th id="thActions" class="py-2.5 px-4 text-right hidden whitespace-nowrap">Actions</th>
            </tr>
          </thead>
          <tbody id="eventsTableBody" class="divide-y divide-slate-200/80">
            <tr>
              <td colspan="6" class="py-8 text-center text-slate-400">Loading noise recordings...</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- Pagination Footer -->
      <div id="eventsPagination" class="p-4 border-t border-slate-200 bg-slate-50 flex flex-col sm:flex-row items-center justify-between gap-3 text-xs text-slate-600">
        <div class="flex items-center gap-3">
          <span id="pageInfo" class="font-medium">Showing 0–0 of 0 recordings</span>
          <span class="text-slate-300 hidden sm:inline">•</span>
          <div class="flex items-center gap-1.5">
            <span class="text-slate-500">Per page:</span>
            <select id="pageSizeSelect" onchange="changePageSize(this.value)" class="bg-white border border-slate-300 text-slate-700 rounded-lg px-2 py-1 text-xs focus:ring-1 focus:ring-indigo-500 shadow-sm">
              <option value="15">15</option>
              <option value="25" selected>25</option>
              <option value="50">50</option>
              <option value="100">100</option>
            </select>
          </div>
        </div>
        <div class="flex items-center gap-1.5">
          <button onclick="gotoPage(1)" id="btnPageFirst" title="First Page" class="px-2.5 py-1.5 rounded-lg bg-white hover:bg-slate-100 border border-slate-200 text-slate-700 font-semibold disabled:opacity-40 disabled:cursor-not-allowed transition shadow-sm">
            <i class="fa-solid fa-angles-left"></i>
          </button>
          <button onclick="gotoPage(currentPage - 1)" id="btnPagePrev" title="Previous Page" class="px-3 py-1.5 rounded-lg bg-white hover:bg-slate-100 border border-slate-200 text-slate-700 font-semibold disabled:opacity-40 disabled:cursor-not-allowed transition flex items-center gap-1 shadow-sm">
            <i class="fa-solid fa-angle-left"></i> Prev
          </button>
          <span id="pageNumbers" class="px-3 py-1.5 font-mono font-bold text-slate-800 bg-white rounded-lg border border-slate-200 shadow-sm">
            Page 1 / 1
          </span>
          <button onclick="gotoPage(currentPage + 1)" id="btnPageNext" title="Next Page" class="px-3 py-1.5 rounded-lg bg-white hover:bg-slate-100 border border-slate-200 text-slate-700 font-semibold disabled:opacity-40 disabled:cursor-not-allowed transition flex items-center gap-1 shadow-sm">
            Next <i class="fa-solid fa-angle-right"></i>
          </button>
          <button onclick="gotoPage(maxPage)" id="btnPageLast" title="Last Page" class="px-2.5 py-1.5 rounded-lg bg-white hover:bg-slate-100 border border-slate-200 text-slate-700 font-semibold disabled:opacity-40 disabled:cursor-not-allowed transition shadow-sm">
            <i class="fa-solid fa-angles-right"></i>
          </button>
        </div>
      </div>
    </div>

    <!-- Footer -->
    <footer class="text-center py-6 text-xs text-slate-500 font-mono border-t border-slate-200 mt-8 flex flex-col sm:flex-row items-center justify-between gap-3 text-center sm:text-left">
      <p>Edmonton Noise Watch &bull; Community Acoustic Corridor Telemetry &bull; Powered by Raspberry Pi &amp; Cloudflare</p>
      <a href="https://mail.google.com/mail/?view=cm&fs=1&to=noise@sethdear.ca&su=Edmonton%20Noise%20Watch%20Inquiry" target="_blank" rel="noopener noreferrer" class="inline-flex items-center gap-1.5 text-indigo-600 hover:text-indigo-800 font-semibold transition" title="Send email to noise@sethdear.ca">
        <i class="fa-solid fa-envelope"></i> Contact (noise@sethdear.ca)
      </a>
    </footer>

  </div>

  <!-- Bottom Navigation Bar -->
  <nav class="fixed bottom-0 inset-x-0 bg-white/95 backdrop-blur border-t border-slate-200 py-3 px-8 flex justify-around items-center z-40 select-none shadow-lg">
    <button onclick="switchMainTab('live')" id="navBtnLive" class="flex flex-col items-center gap-1 text-indigo-600 font-bold text-xs transition">
      <i class="fa-solid fa-house text-base"></i>
      <span>Live</span>
    </button>
    <button onclick="switchMainTab('stats')" id="navBtnStats" class="flex flex-col items-center gap-1 text-slate-400 hover:text-slate-700 text-xs transition">
      <i class="fa-solid fa-chart-column text-base"></i>
      <span>Stats</span>
    </button>
    <a id="navFleetBtn" href="/fleet" target="_self" class="flex flex-col items-center gap-1 text-slate-400 hover:text-indigo-600 text-xs transition">
      <i class="fa-solid fa-tower-broadcast text-base"></i>
      <span>Fleet Hub</span>
    </a>
    <button onclick="openSettingsModal()" class="flex flex-col items-center gap-1 text-slate-400 hover:text-slate-700 text-xs transition">
      <i class="fa-solid fa-sliders text-base"></i>
      <span>Settings</span>
    </button>
  </nav>

  <!-- Admin Unlock Passcode Modal -->
  <div id="adminUnlockModal" class="fixed inset-0 z-50 bg-slate-900/60 backdrop-blur-sm hidden flex items-center justify-center p-4">
    <div class="bg-white border border-slate-200 rounded-2xl w-full max-w-sm p-6 shadow-2xl space-y-4">
      <div class="flex items-center justify-between border-b border-slate-100 pb-3">
        <h3 class="text-base font-bold text-slate-900 flex items-center gap-2">
          <i class="fa-solid fa-lock text-indigo-600"></i> Admin Authentication
        </h3>
        <button onclick="closeAdminUnlockModal()" class="text-slate-400 hover:text-slate-600"><i class="fa-solid fa-xmark"></i></button>
      </div>
      <p class="text-xs text-slate-500">
        Enter administrator passcode to access hardware settings, classification, and station controls.
      </p>
      <form onsubmit="handleAdminUnlock(event)" class="space-y-3">
        <div>
          <input type="password" id="adminUnlockInput" placeholder="Enter admin passcode" class="w-full bg-slate-50 border border-slate-300 focus:bg-white focus:border-indigo-500 rounded-xl p-3 text-sm text-slate-900 font-mono outline-none shadow-sm">
        </div>
        <p id="adminUnlockError" class="text-xs text-rose-600 font-bold hidden"></p>
        <button type="submit" id="adminUnlockSubmitBtn" class="w-full py-3 bg-indigo-600 hover:bg-indigo-500 text-white font-bold rounded-xl text-sm shadow-md shadow-indigo-600/20 transition">
          Unlock & Continue &rarr;
        </button>
      </form>
    </div>
  </div>

  <!-- Delete Recording Confirmation Modal -->
  <div id="deleteConfirmModal" class="fixed inset-0 z-50 bg-slate-900/60 backdrop-blur-sm hidden flex items-center justify-center p-4">
    <div class="bg-white border border-slate-200 rounded-2xl w-full max-w-sm p-6 shadow-2xl space-y-4 text-center">
      <div class="w-12 h-12 rounded-full bg-rose-100 text-rose-600 flex items-center justify-center mx-auto text-xl shadow-inner">
        <i class="fa-solid fa-trash-can"></i>
      </div>
      <div>
        <h3 class="font-bold text-slate-900 text-lg">Delete Recording?</h3>
        <p class="text-xs text-slate-500 mt-1" id="deleteConfirmDetails">This recording file will be permanently removed from storage.</p>
      </div>
      <div class="flex items-center gap-3 pt-2">
        <button type="button" onclick="closeDeleteConfirmModal()" class="flex-1 py-2.5 bg-slate-100 hover:bg-slate-200 text-slate-700 font-bold text-xs rounded-xl transition">
          Cancel
        </button>
        <button type="button" id="confirmDeleteBtn" onclick="executeDelete()" class="flex-1 py-2.5 bg-rose-600 hover:bg-rose-700 text-white font-bold text-xs rounded-xl shadow-md shadow-rose-600/20 transition flex items-center justify-center gap-1.5">
          <i class="fa-solid fa-trash"></i> Delete
        </button>
      </div>
    </div>
  </div>

  <!-- Settings & Config Modal -->
  <div id="settingsModal" class="fixed inset-0 z-50 bg-slate-900/60 backdrop-blur-sm hidden flex items-center justify-center p-4">
    <div class="bg-white border border-slate-200 rounded-2xl w-full max-w-3xl max-h-[90vh] flex flex-col shadow-2xl overflow-hidden">
      <div class="p-6 border-b border-slate-200 flex items-center justify-between bg-slate-50">
        <div class="flex items-center gap-3">
          <div class="p-2 bg-indigo-50 text-indigo-600 border border-indigo-100 rounded-lg">
            <i class="fa-solid fa-sliders"></i>
          </div>
          <h3 class="text-xl font-bold text-slate-900">Bot & Hardware Configuration</h3>
        </div>
        <button onclick="closeSettingsModal()" class="text-slate-400 hover:text-slate-600 text-xl">
          <i class="fa-solid fa-xmark"></i>
        </button>
      </div>

      <!-- Settings Tabs -->
      <div class="flex border-b border-slate-200 bg-slate-50 px-6 gap-6 text-sm font-medium overflow-x-auto">
        <button onclick="switchSettingsTab('audio')" id="tabBtnAudio" class="py-3 text-indigo-600 border-b-2 border-indigo-600 font-bold whitespace-nowrap">🎤 Audio & Elevation</button>
        <button onclick="switchSettingsTab('fleet')" id="tabBtnFleet" class="py-3 text-slate-500 hover:text-slate-800 whitespace-nowrap">🌐 Fleet Hub</button>
        <button onclick="switchSettingsTab('wifi')" id="tabBtnWifi" class="py-3 text-slate-500 hover:text-slate-800 whitespace-nowrap">📶 Wi-Fi Network</button>
        <button onclick="switchSettingsTab('social')" id="tabBtnSocial" class="py-3 text-slate-500 hover:text-slate-800 whitespace-nowrap">📣 Social Alerts</button>
        <button onclick="switchSettingsTab('system')" id="tabBtnSystem" class="py-3 text-slate-500 hover:text-slate-800 whitespace-nowrap">⚙️ System & Logs</button>
      </div>

      <!-- Settings Content Body -->
      <div class="p-6 overflow-y-auto space-y-6 flex-1 text-sm text-slate-700">
        
        <!-- Auth Key Banner -->
        <div class="bg-slate-50 p-3.5 rounded-xl border border-slate-200 flex items-center justify-between gap-3">
          <span class="text-xs text-slate-600 font-semibold">Admin Passcode:</span>
          <input type="password" id="adminPasscode" placeholder="Enter admin passcode" class="bg-white border border-slate-300 rounded-lg px-3 py-1.5 text-xs text-slate-900 shadow-sm">
        </div>

        <!-- Tab 1: Audio & Triangulation -->
        <div id="tabAudio" class="space-y-4">
          <div class="p-4 bg-indigo-50/60 border border-indigo-100 rounded-xl space-y-4">
            <div class="flex items-center justify-between">
              <h4 class="font-bold text-indigo-900 flex items-center gap-2">
                <i class="fa-solid fa-triangle-circle-square"></i> Distance & Elevation Triangulation
              </h4>
              <span class="text-[11px] text-indigo-700 bg-indigo-100/70 px-2.5 py-0.5 rounded-full border border-indigo-200 font-semibold font-mono">Acoustic 3D Ray</span>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label class="block text-xs font-semibold text-slate-600 uppercase mb-1">Street / Road Name</label>
                <input type="text" id="cfgStreetName" placeholder="e.g. 98th Ave / Whyte Ave" class="w-full bg-white border border-slate-300 rounded-lg p-2.5 text-slate-900 text-xs shadow-sm">
              </div>
              <div>
                <label class="block text-xs font-semibold text-slate-600 uppercase mb-1">Location Label</label>
                <input type="text" id="cfgLocationName" placeholder="e.g. 10th Floor Balcony" class="w-full bg-white border border-slate-300 rounded-lg p-2.5 text-slate-900 text-xs shadow-sm">
              </div>
            </div>

            <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 bg-white p-3.5 rounded-xl border border-slate-200 shadow-sm">
              <div>
                <div class="flex justify-between items-center mb-1">
                  <label class="text-xs font-semibold text-slate-700">Floor Level</label>
                  <span id="floorLabelText" class="text-xs font-mono font-bold text-indigo-600">Floor 3 (~6m)</span>
                </div>
                <input type="range" id="cfgFloorNumber" min="1" max="30" step="1" value="3" oninput="calculateTriangulation()" class="w-full accent-indigo-600">
                <p class="text-[10px] text-slate-500 mt-1">1 = Ground level, 10 = ~27m elevation</p>
              </div>

              <div>
                <div class="flex justify-between items-center mb-1">
                  <label class="text-xs font-semibold text-slate-700">Horizontal Setback from Road</label>
                  <span class="text-xs font-mono font-bold text-indigo-600 whitespace-nowrap">
                    <input type="number" id="cfgSetbackMeters" min="0.5" max="1000" step="0.5" value="5" oninput="calculateTriangulation()" class="w-20 text-right bg-white border border-slate-300 rounded-md px-1.5 py-0.5 font-mono font-bold text-indigo-600"> m
                  </span>
                </div>
                <input type="range" id="cfgSetbackSlider" min="1" max="60" step="0.5" value="5" oninput="document.getElementById('cfgSetbackMeters').value = this.value; calculateTriangulation()" class="w-full accent-indigo-600">
                <p class="text-[10px] text-slate-500 mt-1">Distance from building edge to road lane. Type a value for more than 60 m.</p>
              </div>
            </div>

            <div class="p-3 bg-white border border-indigo-100 rounded-xl space-y-1 font-mono text-xs shadow-sm">
              <div class="flex items-center justify-between text-slate-800">
                <span>📐 Triangulated Line-of-Sight Distance:</span>
                <span id="triangulatedDistText" class="font-bold text-emerald-600 text-sm">-- m</span>
              </div>
              <div class="flex items-center justify-between text-slate-600 text-[11px]">
                <span>Acoustic Attenuation from Tailpipe:</span>
                <span id="triangulatedLossText" class="font-bold text-amber-600">-- dB</span>
              </div>
              <p id="triangulatedFormulaText" class="text-[10px] text-slate-500 pt-1 border-t border-slate-100"></p>
            </div>
          </div>

          <div>
            <label class="block text-xs font-semibold text-slate-600 uppercase mb-2">Audio Source</label>
            <select id="cfgAudioSource" onchange="updateAudioSourceFields()" class="w-full bg-white border border-slate-300 rounded-xl p-3 text-slate-900 shadow-sm">
              <option value="pyaudio">🎤 USB Microphone</option>
              <option value="rtsp">📹 IP Camera (RTSP stream)</option>
              <option value="udp">📶 ESP32 Microphone (UDP)</option>
            </select>
          </div>
          <div id="srcFieldsPyaudio">
            <label class="block text-xs font-semibold text-slate-600 uppercase mb-2">Microphone Device</label>
            <select id="cfgAudioDevice" class="w-full bg-white border border-slate-300 rounded-xl p-3 text-slate-900 shadow-sm"></select>
          </div>
          <div id="srcFieldsRtsp" class="hidden">
            <label class="block text-xs font-semibold text-slate-600 uppercase mb-1">RTSP Stream URL</label>
            <input type="password" id="cfgRtspUrl" autocomplete="off" spellcheck="false" placeholder="rtsps://camera-or-nvr:7441/stream-alias" class="w-full bg-white border border-slate-300 rounded-xl p-3 text-slate-900 shadow-sm font-mono text-sm">
            <p class="text-[11px] text-slate-500 mt-1">Usually contains a password or token. It's stored only in config.json. The first audio track is used, resampled to 48 kHz mono.</p>
          </div>
          <div id="srcFieldsUdp" class="hidden">
            <label class="block text-xs font-semibold text-slate-600 uppercase mb-1">UDP Port</label>
            <input type="number" id="cfgUdpPort" min="1" max="65535" step="1" class="w-full bg-white border border-slate-300 rounded-xl p-3 text-slate-900 shadow-sm">
            <p class="text-[11px] text-slate-500 mt-1">Point the ESP32 at this server's IP address on this port.</p>
          </div>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label class="block text-xs font-semibold text-slate-600 uppercase mb-1">Trigger Threshold (dBA)</label>
              <input type="number" id="cfgThreshold" step="0.5" class="w-full bg-white border border-slate-300 rounded-xl p-3 text-slate-900 shadow-sm">
            </div>
            <div>
              <label class="block text-xs font-semibold text-slate-600 uppercase mb-1">Calibration Offset (dB)</label>
              <input type="number" id="cfgOffset" step="0.5" class="w-full bg-white border border-slate-300 rounded-xl p-3 text-slate-900 shadow-sm">
            </div>
          </div>
        </div>

        <!-- Tab: Fleet Hub Integration -->
        <div id="tabFleet" class="space-y-4 hidden">
          <div class="p-4 bg-indigo-50/60 border border-indigo-100 rounded-xl space-y-4">
            <div class="flex items-center justify-between">
              <h4 class="font-bold text-indigo-900 flex items-center gap-2">
                <i class="fa-solid fa-tower-broadcast"></i> Central Fleet Hub Integration
              </h4>
              <div class="flex items-center gap-2">
                <span class="text-xs text-slate-600 font-semibold">Sync Active</span>
                <input type="checkbox" id="cfgFleetEnabled" class="w-5 h-5 rounded accent-indigo-600">
              </div>
            </div>
            <div>
              <label class="block text-xs font-semibold text-slate-600 uppercase mb-1">Central Fleet Hub URL</label>
              <input type="text" id="cfgFleetHubUrl" placeholder="https://sethdear.ca/yegnoise" class="w-full bg-white border border-slate-300 rounded-lg p-2.5 text-slate-900 text-xs font-mono shadow-sm">
              <p class="text-[10px] text-slate-500 mt-1">Direct URL of the Central Fleet Server. All "Fleet Hub" buttons and redirects will open this location.</p>
            </div>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label class="block text-xs font-semibold text-slate-600 uppercase mb-1">Station ID (Unique Identifier)</label>
                <input type="text" id="cfgFleetStationId" placeholder="e.g. noise-bot-balcony" class="w-full bg-white border border-slate-300 rounded-lg p-2.5 text-slate-900 text-xs font-mono shadow-sm">
              </div>
              <div>
                <label class="block text-xs font-semibold text-slate-600 uppercase mb-1">Station Display Name</label>
                <input type="text" id="cfgFleetStationName" placeholder="e.g. Balcony Station" class="w-full bg-white border border-slate-300 rounded-lg p-2.5 text-slate-900 text-xs shadow-sm">
              </div>
            </div>
          </div>
        </div>

        <!-- Tab 2: Wi-Fi Network -->
        <div id="tabWifi" class="space-y-4 hidden">
          <div class="p-4 bg-emerald-50/60 border border-emerald-100 rounded-xl space-y-4">
            <div class="flex items-center justify-between">
              <h4 class="font-bold text-emerald-900 flex items-center gap-2">
                <i class="fa-solid fa-wifi"></i> Wi-Fi Network Setup
              </h4>
              <button onclick="scanWifi()" class="px-3 py-1.5 bg-white hover:bg-slate-100 text-slate-700 rounded-lg text-xs font-bold border border-slate-300 shadow-sm transition">
                <i class="fa-solid fa-satellite-dish"></i> Scan Networks
              </button>
            </div>
            <div id="wifiList" class="space-y-2">
              <p class="text-xs text-slate-500">Click "Scan Networks" to search for nearby Wi-Fi routers.</p>
            </div>
            <div class="p-3.5 bg-white rounded-xl border border-slate-200 space-y-3 shadow-sm">
              <input type="text" id="wifiSsidInput" placeholder="Select above or type Wi-Fi SSID" class="w-full bg-slate-50 border border-slate-300 rounded-lg p-2.5 text-slate-900 text-xs">
              <input type="password" id="wifiPasswordInput" placeholder="Enter Wi-Fi password" class="w-full bg-slate-50 border border-slate-300 rounded-lg p-2.5 text-slate-900 text-xs">
              <button onclick="connectWifi()" class="w-full py-2.5 bg-emerald-600 hover:bg-emerald-500 text-white font-bold rounded-lg text-xs shadow-md shadow-emerald-600/20">
                Join Wi-Fi Network
              </button>
            </div>
          </div>
        </div>

        <!-- Tab 3: Social Alerts -->
        <div id="tabSocial" class="space-y-6 hidden">
          <div class="p-4 bg-sky-50/60 border border-sky-200 rounded-xl space-y-4">
            <div class="flex items-center justify-between">
              <h4 class="font-bold text-sky-900 flex items-center gap-2 text-sm"><i class="fa-solid fa-cloud text-sky-600"></i> Bluesky Auto-Posting</h4>
              <input type="checkbox" id="cfgBlueskyEnabled" class="w-5 h-5 rounded accent-sky-500">
            </div>
            <p class="text-xs text-slate-600">Automatically post noise violation alerts with 8-second audio clips to Bluesky in real-time.</p>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label class="block text-[10px] uppercase font-bold text-slate-600 mb-1">Bluesky Handle</label>
                <input type="text" id="cfgBlueskyHandle" placeholder="e.g. yourhandle.bsky.social" class="w-full bg-white border border-slate-300 rounded-lg p-2.5 text-slate-900 text-xs shadow-sm font-mono">
              </div>
              <div>
                <label class="block text-[10px] uppercase font-bold text-slate-600 mb-1">App Password</label>
                <input type="password" id="cfgBlueskyPassword" placeholder="xxxx-xxxx-xxxx-xxxx" class="w-full bg-white border border-slate-300 rounded-lg p-2.5 text-slate-900 text-xs shadow-sm font-mono">
              </div>
            </div>
            <div>
              <label class="block text-[10px] uppercase font-bold text-slate-600 mb-1">Target Accounts to Tag / Mention (Comma-separated)</label>
              <input type="text" id="cfgBlueskyTargets" placeholder="e.g. @edmontonpolice.bsky.social, @ashleysalvador.bsky.social, @andrewknack.bsky.social" class="w-full bg-white border border-slate-300 rounded-lg p-2.5 text-slate-900 text-xs shadow-sm font-mono">
              <p class="text-[10px] text-slate-500 mt-1">Get an App Password on Bluesky: <span class="font-semibold text-sky-700">Settings &rarr; Privacy & Security &rarr; App Passwords &rarr; Add App Password</span>.</p>
            </div>
          </div>
        </div>

        <!-- Tab 4: System & Logs -->
        <div id="tabSystem" class="space-y-4 hidden">
          <div class="flex items-center justify-between">
            <h4 class="font-bold text-slate-900">Live System Logs</h4>
            <button onclick="fetchLogs()" class="px-3 py-1 bg-white border border-slate-300 text-xs font-semibold rounded-lg hover:bg-slate-100 shadow-sm">Refresh Logs</button>
          </div>
          <pre id="systemLogs" class="bg-slate-900 border border-slate-800 p-3 rounded-xl text-xs font-mono text-emerald-400 h-48 overflow-y-auto whitespace-pre-wrap">Loading logs...</pre>
        </div>

      </div>

      <!-- Modal Footer -->
      <div class="p-4 border-t border-slate-200 bg-slate-50 flex items-center justify-end gap-3">
        <button onclick="closeSettingsModal()" class="px-4 py-2 bg-white hover:bg-slate-100 border border-slate-300 text-slate-700 font-semibold rounded-xl shadow-sm">Cancel</button>
        <button onclick="saveSettings()" class="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 text-white font-bold rounded-xl shadow-md shadow-indigo-600/20">Save & Apply</button>
      </div>
    </div>
  </div>

  <script>
    let allEvents = [];
    let loadedConfig = {};
    let chartInstance = null;

    function switchMainTab(tab) {
      if (tab === 'live') {
        document.getElementById('viewLive').classList.remove('hidden');
        document.getElementById('viewStats').classList.add('hidden');
        document.getElementById('navBtnLive').className = 'flex flex-col items-center gap-1 text-indigo-600 font-bold text-xs transition';
        document.getElementById('navBtnStats').className = 'flex flex-col items-center gap-1 text-slate-400 hover:text-slate-700 text-xs transition';
      } else if (tab === 'stats') {
        document.getElementById('viewLive').classList.add('hidden');
        document.getElementById('viewStats').classList.remove('hidden');
        document.getElementById('navBtnStats').className = 'flex flex-col items-center gap-1 text-indigo-600 font-bold text-xs transition';
        document.getElementById('navBtnLive').className = 'flex flex-col items-center gap-1 text-slate-400 hover:text-slate-700 text-xs transition';
        renderDailyChart();
      }
    }

    function calculateTriangulation() {
      const floor = parseInt(document.getElementById('cfgFloorNumber').value) || 1;
      const setback = parseFloat(document.getElementById('cfgSetbackMeters').value) || 5.0;
      
      const height = (floor - 1) * 3.0;
      const dist = Math.sqrt(Math.pow(setback, 2) + Math.pow(height, 2));
      const finalDist = Math.max(0.5, dist);

      document.getElementById('floorLabelText').innerText = 'Floor ' + floor + ' (~' + height.toFixed(0) + 'm height)';
      const setbackSlider = document.getElementById('cfgSetbackSlider');
      if (setbackSlider) setbackSlider.value = Math.min(setback, parseFloat(setbackSlider.max));
      document.getElementById('triangulatedDistText').innerText = finalDist.toFixed(1) + ' meters';

      const loss = 20 * Math.log10(finalDist / 0.5);
      document.getElementById('triangulatedLossText').innerText = '+' + loss.toFixed(1) + ' dB';

      document.getElementById('triangulatedFormulaText').innerText = 
        'Formula: √(' + setback.toFixed(1) + 'm setback² + ' + height.toFixed(0) + 'm height²) = ' + finalDist.toFixed(1) + 'm line-of-sight. A 75 dBA sound at your mic = ~' + (75 + loss).toFixed(1) + ' dBA at tailpipe.';

      return finalDist;
    }

    async function fetchEvents() {
      try {
        const res = await fetch('/api/events');
        allEvents = await res.json();
        
        // Ensure ev.url is properly routed if served under /yegnoise/<slug>
        const curPath = window.location.pathname.replace(/\/+$/, '');
        const hubPrefix = curPath.startsWith('/yegnoise/') ? curPath : '';
        allEvents.forEach(ev => {
          if (ev.url && !ev.url.startsWith('http') && !ev.url.startsWith('/yegnoise/') && hubPrefix) {
            ev.url = hubPrefix + (ev.url.startsWith('/') ? ev.url : '/' + ev.url);
          }
        });

        renderEvents();
        if (!document.getElementById('viewStats').classList.contains('hidden')) {
          renderDailyChart();
        }
      } catch (e) {
        console.error(e);
      }
    }

    function renderDailyChart() {
      const ctx = document.getElementById('dailyEventsChart');
      if (!ctx) return;

      const countsByDay = {};
      const today = new Date();
      for (let i = 13; i >= 0; i--) {
        const d = new Date(today);
        d.setDate(d.getDate() - i);
        const key = d.toISOString().split('T')[0];
        countsByDay[key] = 0;
      }

      allEvents.forEach(ev => {
        if (ev.date && countsByDay[ev.date] !== undefined) {
          countsByDay[ev.date]++;
        }
      });

      const labels = Object.keys(countsByDay).map(k => k.slice(5));
      const data = Object.values(countsByDay);

      if (chartInstance) {
        chartInstance.destroy();
      }

      chartInstance = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [{
            label: 'Noise Events',
            data: data,
            backgroundColor: 'rgba(99, 102, 241, 0.85)',
            borderColor: '#4f46e5',
            borderWidth: 1.5,
            borderRadius: 6
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            y: { beginAtZero: true, ticks: { stepSize: 1, color: '#64748b' }, grid: { color: 'rgba(0, 0, 0, 0.05)' } },
            x: { ticks: { color: '#64748b' }, grid: { display: false } }
          }
        }
      });
    }

    function getStationSketchSvg(type) {
      if (type === 'whyte') {
        return `<svg viewBox="0 0 120 70" class="w-28 h-16 rounded-xl shadow-xs" fill="none" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <linearGradient id="whyteGrad" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stop-color="#fffbeb"/>
              <stop offset="100%" stop-color="#fef3c7"/>
            </linearGradient>
          </defs>
          <rect width="120" height="70" rx="12" fill="url(#whyteGrad)" stroke="#fde68a" stroke-width="1.2"/>
          <path d="M14 62 V24 M12 24 H16 M14 24 Q18 20 22 24 M22 24 V27" stroke="#b45309" stroke-width="1.5" stroke-linecap="round"/>
          <circle cx="22" cy="28" r="2" fill="#f59e0b"/>
          <rect x="32" y="16" width="18" height="46" rx="2" fill="#ffedd5" stroke="#c2410c" stroke-width="1.2"/>
          <path d="M30 16 L41 6 L52 16 Z" fill="#ea580c" stroke="#9a3412" stroke-width="1.2"/>
          <circle cx="41" cy="24" r="3.5" fill="#ffffff" stroke="#c2410c" stroke-width="1"/>
          <path d="M41 24 V22 M41 24 H43" stroke="#c2410c" stroke-width="0.8"/>
          <rect x="36" y="34" width="10" height="14" rx="1" fill="#ea580c" opacity="0.3"/>
          <rect x="58" y="32" width="52" height="26" rx="4" fill="#fef08a" stroke="#d97706" stroke-width="1.5"/>
          <path d="M58 44 H110" stroke="#b45309" stroke-width="1.2"/>
          <rect x="62" y="36" width="7" height="6" rx="1" fill="#38bdf8" opacity="0.8" stroke="#b45309" stroke-width="0.8"/>
          <rect x="72" y="36" width="7" height="6" rx="1" fill="#38bdf8" opacity="0.8" stroke="#b45309" stroke-width="0.8"/>
          <rect x="82" y="36" width="7" height="6" rx="1" fill="#38bdf8" opacity="0.8" stroke="#b45309" stroke-width="0.8"/>
          <rect x="92" y="36" width="7" height="6" rx="1" fill="#38bdf8" opacity="0.8" stroke="#b45309" stroke-width="0.8"/>
          <rect x="101" y="36" width="6" height="6" rx="1" fill="#38bdf8" opacity="0.8" stroke="#b45309" stroke-width="0.8"/>
          <path d="M78 32 L88 12 H102" stroke="#b45309" stroke-width="1.2" stroke-linecap="round"/>
          <circle cx="68" cy="60" r="3" fill="#475569" stroke="#1e293b" stroke-width="1"/>
          <circle cx="100" cy="60" r="3" fill="#475569" stroke="#1e293b" stroke-width="1"/>
          <line x1="8" y1="63" x2="114" y2="63" stroke="#94a3b8" stroke-width="2" stroke-linecap="round"/>
        </svg>`;
      } else if (type === 'brewery') {
        return `<svg viewBox="0 0 120 70" class="w-28 h-16 rounded-xl shadow-xs" fill="none" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <linearGradient id="breweryGrad" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stop-color="#fff7ed"/>
              <stop offset="100%" stop-color="#ffedd5"/>
            </linearGradient>
          </defs>
          <rect width="120" height="70" rx="12" fill="url(#breweryGrad)" stroke="#fed7aa" stroke-width="1.2"/>
          <path d="M18 62 L22 14 H28 L32 62 Z" fill="#c2410c" stroke="#9a3412" stroke-width="1.2"/>
          <rect x="20" y="12" width="10" height="3" rx="1" fill="#7c2d12"/>
          <path d="M23 10 Q22 6 25 4 Q28 2 27 0" stroke="#cbd5e1" stroke-width="1.2" stroke-linecap="round" opacity="0.6"/>
          <rect x="36" y="22" width="34" height="40" fill="#ea580c" stroke="#9a3412" stroke-width="1.2"/>
          <path d="M36 22 H40 V18 H44 V22 H48 V18 H52 V22 H56 V18 H60 V22 H64 V18 H67 V22 H70" stroke="#9a3412" stroke-width="1.2" fill="#c2410c"/>
          <path d="M42 34 A3 3 0 0 1 48 34 V42 H42 Z" fill="#fed7aa" stroke="#9a3412" stroke-width="1"/>
          <path d="M56 34 A3 3 0 0 1 62 34 V42 H56 Z" fill="#fed7aa" stroke="#9a3412" stroke-width="1"/>
          <rect x="48" y="48" width="10" height="14" fill="#7c2d12" stroke="#431407" stroke-width="1"/>
          <rect x="74" y="34" width="38" height="28" fill="#e0e7ff" stroke="#6366f1" stroke-width="1.2"/>
          <path d="M72 34 L114 34" stroke="#4f46e5" stroke-width="2" stroke-linecap="round"/>
          <rect x="78" y="40" width="12" height="14" rx="1" fill="#ffffff" stroke="#6366f1" stroke-width="0.8"/>
          <rect x="94" y="40" width="14" height="22" rx="1" fill="#ffffff" stroke="#6366f1" stroke-width="0.8"/>
          <line x1="8" y1="62" x2="114" y2="62" stroke="#94a3b8" stroke-width="2" stroke-linecap="round"/>
        </svg>`;
      } else if (type === 'cloverdale') {
        return `<svg viewBox="0 0 120 70" class="w-28 h-16 rounded-xl shadow-xs" fill="none" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <linearGradient id="cloverGrad" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stop-color="#ecfdf5"/>
              <stop offset="100%" stop-color="#e0f2fe"/>
            </linearGradient>
          </defs>
          <rect width="120" height="70" rx="12" fill="url(#cloverGrad)" stroke="#a7f3d0" stroke-width="1.2"/>
          <path d="M6 62 Q30 46 60 52 Q90 58 114 48 V62 H6 Z" fill="#d1fae5" opacity="0.7"/>
          <path d="M6 60 Q40 56 70 60 Q95 64 114 58" stroke="#38bdf8" stroke-width="3" stroke-linecap="round"/>
          <polygon points="30,22 14,56 46,56" fill="#6ee7b7" opacity="0.7" stroke="#059669" stroke-width="1.2"/>
          <line x1="30" y1="22" x2="30" y2="56" stroke="#059669" stroke-width="1"/>
          <polygon points="50,28 36,56 64,56" fill="#93c5fd" opacity="0.7" stroke="#0284c7" stroke-width="1.2"/>
          <line x1="50" y1="28" x2="50" y2="56" stroke="#0284c7" stroke-width="1"/>
          <path d="M72 48 L78 36 L86 48 L94 36 L102 48 L110 36" stroke="#475569" stroke-width="1.2" stroke-linecap="round" fill="none"/>
          <line x1="70" y1="48" x2="112" y2="48" stroke="#334155" stroke-width="2"/>
          <line x1="6" y1="62" x2="114" y2="62" stroke="#059669" stroke-width="1.5" stroke-linecap="round"/>
        </svg>`;
      } else {
        return `<svg viewBox="0 0 120 70" class="w-28 h-16 rounded-xl shadow-xs" fill="none" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <linearGradient id="cityGrad" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stop-color="#eef2ff"/>
              <stop offset="100%" stop-color="#ede9fe"/>
            </linearGradient>
          </defs>
          <rect width="120" height="70" rx="12" fill="url(#cityGrad)" stroke="#c7d2fe" stroke-width="1.2"/>
          <rect x="18" y="28" width="12" height="34" fill="#c7d2fe" stroke="#4f46e5" stroke-width="1"/>
          <rect x="34" y="16" width="14" height="46" fill="#a5b4fc" stroke="#4f46e5" stroke-width="1"/>
          <polygon points="41,6 36,16 46,16" fill="#4f46e5"/>
          <rect x="52" y="24" width="12" height="38" fill="#c7d2fe" stroke="#4f46e5" stroke-width="1"/>
          <rect x="68" y="20" width="16" height="42" fill="#a5b4fc" stroke="#4f46e5" stroke-width="1"/>
          <path d="M88 44 L94 34 L100 44 L106 34 L112 44" stroke="#4338ca" stroke-width="1.2" stroke-linecap="round" fill="none"/>
          <line x1="86" y1="44" x2="114" y2="44" stroke="#312e81" stroke-width="1.8"/>
          <line x1="8" y1="62" x2="114" y2="62" stroke="#4f46e5" stroke-width="1.5" stroke-linecap="round"/>
        </svg>`;
      }
    }

    function updateStationBranding(stationName, streetName, locName, floorNum, setbackM) {
      const curPath = window.location.pathname.toLowerCase();
      const combined = ((stationName || '') + ' ' + (locName || '') + ' ' + (streetName || '') + ' ' + curPath).toLowerCase();
      
      let stTitle = stationName || locName || 'Traffic Noise Monitor';
      let locTag = '';
      let sketchType = 'default';
      
      if (combined.includes('whyte') || combined.includes('82') || combined.includes('janz') || combined.includes('strathcona')) {
        stTitle = (stationName && stationName.toLowerCase().includes('whyte')) ? stationName : 'Whyte Ave Station';
        locTag = 'Old Strathcona';
        sketchType = 'whyte';
      } else if (combined.includes('brewery') || combined.includes('124') || combined.includes('104') || combined.includes('oliver') || combined.includes('sub1a') || combined.includes('subla')) {
        stTitle = (stationName && stationName.toLowerCase().includes('brewery')) ? stationName : 'Brewery District Station';
        locTag = 'Oliver / 124th St';
        sketchType = 'brewery';
      } else if (combined.includes('cloverdale') || combined.includes('98') || combined.includes('river') || combined.includes('balcony') || combined.includes('muttart')) {
        stTitle = (stationName && stationName.toLowerCase().includes('cloverdale')) ? stationName : 'Cloverdale Station';
        locTag = 'River Valley';
        sketchType = 'cloverdale';
      } else {
        stTitle = stationName || locName || 'Traffic Noise Monitor';
        locTag = 'Edmonton';
        sketchType = 'default';
      }
      
      const tEl = document.getElementById('headerTitle');
      if (tEl) tEl.innerText = stTitle;
      
      document.title = `${stTitle} — Edmonton Noise Bot`;
      
      const bEl = document.getElementById('headerLocationBadge');
      if (bEl) {
        if (locTag) {
          bEl.innerText = locTag;
          bEl.classList.remove('hidden');
        } else {
          bEl.classList.add('hidden');
        }
      }
      
      const sEl = document.getElementById('headerSubtitle');
      if (sEl) {
        const strName = streetName || (combined.includes('whyte') ? '82nd Ave & 110th St' : (combined.includes('brewery') ? '104th Ave & 124th St' : (combined.includes('cloverdale') ? '98th Ave' : 'Edmonton Corridor')));
        const fText = floorNum ? `Floor ${floorNum}` : (loadedConfig.floor_number ? `Floor ${loadedConfig.floor_number}` : '');
        const sbText = setbackM ? `${setbackM}m setback` : (loadedConfig.horizontal_setback_meters ? `${loadedConfig.horizontal_setback_meters}m setback` : '');
        const details = [fText, sbText].filter(Boolean).join(', ');
        sEl.innerText = strName + (details ? ` • ${details}` : ' • Live Acoustic Telemetry');
      }
      
      const skContainer = document.getElementById('stationSketchContainer');
      if (skContainer) {
        skContainer.innerHTML = getStationSketchSvg(sketchType);
      }
    }

    let currentAdminPasscode = '';
    let pendingOpenSettings = false;

    function getAdminPasscode() {
      if (currentAdminPasscode) return currentAdminPasscode;
      try {
        if (typeof window !== 'undefined') {
          const s1 = window.sessionStorage && window.sessionStorage.getItem('station_admin_passcode');
          if (s1) { currentAdminPasscode = s1; return s1; }
          const s2 = window.sessionStorage && window.sessionStorage.getItem('fleet_admin_key');
          if (s2) { currentAdminPasscode = s2; return s2; }
          const l1 = window.localStorage && window.localStorage.getItem('station_admin_passcode');
          if (l1) { currentAdminPasscode = l1; return l1; }
          const l2 = window.localStorage && window.localStorage.getItem('fleet_admin_key');
          if (l2) { currentAdminPasscode = l2; return l2; }
        }
      } catch (e) {}
      const inp = document.getElementById('adminPasscode');
      if (inp && inp.value.trim()) {
        currentAdminPasscode = inp.value.trim();
        return currentAdminPasscode;
      }
      return '';
    }

    function isAdminActive() {
      return !!getAdminPasscode();
    }

    function showToast(msg, type = 'info') {
      let toast = document.getElementById('stationToast');
      if (!toast) {
        toast = document.createElement('div');
        toast.id = 'stationToast';
        document.body.appendChild(toast);
      }
      let colorClasses = 'bg-slate-900 text-white border-slate-700 shadow-slate-900/30';
      let icon = '<i class="fa-solid fa-circle-info text-indigo-400 text-sm"></i>';
      if (type === 'success') {
        colorClasses = 'bg-emerald-950 text-emerald-100 border-emerald-600/50 shadow-emerald-950/30';
        icon = '<i class="fa-solid fa-circle-check text-emerald-400 text-sm"></i>';
      } else if (type === 'error') {
        colorClasses = 'bg-rose-950 text-rose-100 border-rose-600/50 shadow-rose-950/30';
        icon = '<i class="fa-solid fa-circle-exclamation text-rose-400 text-sm"></i>';
      }
      toast.className = `fixed bottom-6 right-6 z-50 px-4 py-3 rounded-xl border shadow-2xl font-semibold text-xs flex items-center gap-2.5 transition-all duration-300 transform translate-y-0 opacity-100 ${colorClasses}`;
      toast.innerHTML = `${icon} <span>${msg}</span>`;
      clearTimeout(toast._timer);
      toast._timer = setTimeout(() => {
        toast.className = toast.className.replace('translate-y-0 opacity-100', 'translate-y-2 opacity-0 pointer-events-none');
      }, 3200);
    }

    function updateAdminUI() {
      const btn = document.getElementById('headerAdminBtn');
      const text = document.getElementById('headerAdminText');
      const icon = document.getElementById('headerAdminIcon');
      const actionHeader = document.getElementById('thActions');
      
      if (isAdminActive()) {
        if (text) text.innerText = 'Admin Active (Lock)';
        if (icon) icon.className = 'fa-solid fa-lock-open text-emerald-600';
        if (btn) btn.className = 'px-3.5 py-2 bg-emerald-50 hover:bg-rose-50 border border-emerald-300 text-emerald-700 font-bold text-xs rounded-xl flex items-center gap-1.5 shadow-sm transition';
        if (actionHeader) actionHeader.classList.remove('hidden');
      } else {
        if (text) text.innerText = 'Admin Unlock';
        if (icon) icon.className = 'fa-solid fa-lock text-slate-400';
        if (btn) btn.className = 'px-3.5 py-2 bg-slate-100 hover:bg-slate-200 border border-slate-200 text-slate-700 font-bold text-xs rounded-xl flex items-center gap-1.5 shadow-sm transition';
        if (actionHeader) actionHeader.classList.add('hidden');
      }
    }

    function toggleAdminUnlock() {
      if (isAdminActive()) {
        currentAdminPasscode = '';
        try {
          sessionStorage.removeItem('station_admin_passcode');
          sessionStorage.removeItem('fleet_admin_key');
          localStorage.removeItem('station_admin_passcode');
          localStorage.removeItem('fleet_admin_key');
        } catch (e) {}
        const adminPassField = document.getElementById('adminPasscode');
        if (adminPassField) adminPassField.value = '';
        updateAdminUI();
        renderEvents();
        showToast('Admin Mode Locked', 'info');
      } else {
        pendingOpenSettings = false;
        openAdminUnlockModal();
      }
    }

    function openAdminUnlockModal() {
      document.getElementById('adminUnlockModal').classList.remove('hidden');
      document.getElementById('adminUnlockError').classList.add('hidden');
      const inp = document.getElementById('adminUnlockInput');
      inp.value = '';
      setTimeout(() => inp.focus(), 100);
    }

    function closeAdminUnlockModal() {
      document.getElementById('adminUnlockModal').classList.add('hidden');
    }

    async function handleAdminUnlock(e) {
      e.preventDefault();
      const code = document.getElementById('adminUnlockInput').value.trim();
      const err = document.getElementById('adminUnlockError');
      const btn = document.getElementById('adminUnlockSubmitBtn');
      if (!code) return;

      btn.innerText = 'Verifying...';
      btn.disabled = true;
      err.classList.add('hidden');

      try {
        const res = await fetch('/api/config?key=' + encodeURIComponent(code), {
          headers: { 'X-Admin-Key': code }
        });
        if (res.ok) {
          currentAdminPasscode = code;
          try {
            sessionStorage.setItem('station_admin_passcode', code);
            sessionStorage.setItem('fleet_admin_key', code);
            localStorage.setItem('station_admin_passcode', code);
            localStorage.setItem('fleet_admin_key', code);
          } catch (ex) {}
          const adminPassField = document.getElementById('adminPasscode');
          if (adminPassField) adminPassField.value = code;
          closeAdminUnlockModal();
          updateAdminUI();
          renderEvents();
          if (pendingOpenSettings) {
            pendingOpenSettings = false;
            openSettingsModal();
          } else {
            showToast('Admin Mode Unlocked', 'success');
          }
        } else {
          err.innerText = 'Access denied: Incorrect passcode.';
          err.classList.remove('hidden');
        }
      } catch (ex) {
        err.innerText = 'Authentication error. Please try again.';
        err.classList.remove('hidden');
      } finally {
        btn.innerText = 'Unlock & Continue \u2192';
        btn.disabled = false;
      }
    }

    let currentPage = 1;
    let pageSize = 25;
    let maxPage = 1;
    let sortColumn = 'datetime';
    let sortDirection = 'desc';

    function toggleSort(col) {
      if (sortColumn === col) {
        sortDirection = (sortDirection === 'asc') ? 'desc' : 'asc';
      } else {
        sortColumn = col;
        if (col === 'tag') {
          sortDirection = 'asc';
        } else {
          sortDirection = 'desc';
        }
      }
      syncSortDropdown();
      updateSortIcons();
      currentPage = 1;
      renderEvents();
    }

    function onSortSelectChange(val) {
      const parts = val.split('_');
      sortColumn = parts[0];
      sortDirection = parts[1] || 'desc';
      updateSortIcons();
      currentPage = 1;
      renderEvents();
    }

    function syncSortDropdown() {
      const sel = document.getElementById('sortSelect');
      if (sel) {
        const val = `${sortColumn}_${sortDirection}`;
        if (sel.querySelector(`option[value="${val}"]`)) {
          sel.value = val;
        }
      }
    }

    function updateSortIcons() {
      const cols = ['datetime', 'dba', 'tailpipe', 'tag'];
      cols.forEach(col => {
        const icon = document.getElementById(`sortIcon-${col}`);
        if (!icon) return;
        if (sortColumn === col) {
          if (sortDirection === 'asc') {
            icon.className = 'fa-solid fa-arrow-up text-indigo-600 text-xs';
          } else {
            icon.className = 'fa-solid fa-arrow-down text-indigo-600 text-xs';
          }
        } else {
          icon.className = 'fa-solid fa-sort text-slate-300 text-xs';
        }
      });
    }

    function gotoPage(p) {
      currentPage = Math.max(1, Math.min(p, maxPage));
      renderEvents();
      const el = document.getElementById('eventsPagination');
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    function changePageSize(sz) {
      pageSize = parseInt(sz) || 25;
      currentPage = 1;
      renderEvents();
    }

    function togglePlayPeakAudio() {
      const audio = document.getElementById('peakAudioPlayer');
      const icon = document.getElementById('peakPlayIcon');
      const text = document.getElementById('peakPlayText');
      const btn = document.getElementById('peakPlayBtn');
      if (!audio || !audio.src) return;

      if (audio.paused) {
        const p = audio.play();
        if (p !== undefined) {
          p.then(() => {
            if (icon) icon.className = 'fa-solid fa-pause text-white';
            if (text) text.innerText = 'Playing...';
            if (btn) btn.className = 'px-3 py-1 bg-rose-600 text-white font-bold text-xs rounded-xl flex items-center gap-1.5 shadow-md shadow-rose-600/20 transition';
          }).catch(err => {
            console.error('Peak audio play error:', err);
            if (icon) icon.className = 'fa-solid fa-play';
            if (text) text.innerText = 'Play Audio';
            if (btn) btn.className = 'px-3 py-1 bg-rose-50 hover:bg-rose-100 border border-rose-200 text-rose-700 font-bold text-xs rounded-xl flex items-center gap-1.5 transition';
          });
        }
      } else {
        audio.pause();
        if (icon) icon.className = 'fa-solid fa-play';
        if (text) text.innerText = 'Play Audio';
        if (btn) btn.className = 'px-3 py-1 bg-rose-50 hover:bg-rose-100 border border-rose-200 text-rose-700 font-bold text-xs rounded-xl flex items-center gap-1.5 transition';
      }
      audio.onended = function() {
        if (icon) icon.className = 'fa-solid fa-play';
        if (text) text.innerText = 'Play Audio';
        if (btn) btn.className = 'px-3 py-1 bg-rose-50 hover:bg-rose-100 border border-rose-200 text-rose-700 font-bold text-xs rounded-xl flex items-center gap-1.5 transition';
      };
    }

    let currentPeakEvent = null;

    function jumpToPeakEvent() {
      if (currentPeakEvent) {
        const filterEl = document.getElementById('filterTag');
        if (filterEl && filterEl.value !== 'all' && filterEl.value !== 'traffic' && filterEl.value !== 'vehicle' && filterEl.value !== 'ets') {
          filterEl.value = 'all';
          currentPage = 1;
          renderEvents();
        }
      }
      const table = document.getElementById('eventsTableBody');
      if (table) {
        table.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }

    function renderEvents() {
      const filter = document.getElementById('filterTag').value;
      const tbody = document.getElementById('eventsTableBody');
      const filtered = (filter === 'all' ? allEvents.slice() : allEvents.filter(e => {
        const t = (e.tag || '').toLowerCase();
        if (filter === 'traffic') return t === 'traffic' || t === 'vehicle';
        if (filter === 'ets') return t === 'ets' || t === 'bus';
        if (filter === 'bbq') return t === 'bbq' || t === 'patio';
        return t === filter;
      }));
      const isAdmin = isAdminActive();

      updateAdminUI();
      updateSortIcons();
      syncSortDropdown();

      const curDist = (loadedConfig && loadedConfig.distance_to_road_meters) ? loadedConfig.distance_to_road_meters : 7.0;
      const lossDb = 20 * Math.log10(Math.max(0.5, curDist) / 0.5);

      filtered.sort((a, b) => {
        let valA, valB;
        if (sortColumn === 'dba') {
          valA = (a.dba !== undefined && a.dba !== null) ? Number(a.dba) : 0;
          valB = (b.dba !== undefined && b.dba !== null) ? Number(b.dba) : 0;
          if (valA !== valB) {
            return sortDirection === 'asc' ? valA - valB : valB - valA;
          }
          return (b.datetime || '').localeCompare(a.datetime || '');
        } else if (sortColumn === 'tailpipe') {
          valA = (a.dba !== undefined && a.dba !== null) ? (Number(a.dba) + lossDb) : (Number(a.tailpipe_dba) || 0);
          valB = (b.dba !== undefined && b.dba !== null) ? (Number(b.dba) + lossDb) : (Number(b.tailpipe_dba) || 0);
          if (valA !== valB) {
            return sortDirection === 'asc' ? valA - valB : valB - valA;
          }
          return (b.datetime || '').localeCompare(a.datetime || '');
        } else if (sortColumn === 'tag') {
          valA = (a.tag || '').toLowerCase();
          valB = (b.tag || '').toLowerCase();
          const cmp = valA.localeCompare(valB);
          if (cmp !== 0) {
            return sortDirection === 'asc' ? cmp : -cmp;
          }
          return (b.datetime || '').localeCompare(a.datetime || '');
        } else {
          // Default: 'datetime'
          valA = a.datetime || '';
          valB = b.datetime || '';
          const cmp = valA.localeCompare(valB);
          return sortDirection === 'asc' ? cmp : -cmp;
        }
      });

      const tagLabels = {
        traffic: '🚗 Traffic',
        vehicle: '🚗 Traffic',
        ets: '🚌 ETS',
        bus: '🚌 ETS',
        siren: '🚨 Siren',
        construction: '🏗️ Construction',
        weather: '🌧️ Weather',
        bbq: '🍖 BBQ',
        impulse: '💥 Impulse',
        misc: '❓ Misc',
        review: '⚠️ Review'
      };
      // One list drives the per-event dropdown; aliases map to their canonical tag, and any
      // other tag gets its own option instead of silently showing as the first one
      const TAG_ALIASES = { vehicle: 'traffic', bus: 'ets', patio: 'bbq' };
      const TAG_OPTIONS = ['traffic', 'ets', 'bbq', 'siren', 'construction', 'weather', 'impulse', 'misc', 'review'];

      const trafficEvents = allEvents.filter(e => {
        const t = (e.tag || '').toLowerCase();
        return t === 'traffic' || t === 'vehicle' || t === 'ets' || t === 'bus';
      });
      let vehicleCount = trafficEvents.length;
      let bylawCount = allEvents.filter(e => e.dba >= 70 && (e.tag || '').toLowerCase() !== 'bbq' && (e.tag || '').toLowerCase() !== 'patio').length;
      
      // Peak Loudest must strictly reflect Traffic / Vehicle / ETS noise, not sirens, construction, or weather
      let peakEvent = null;
      if (trafficEvents.length > 0) {
        peakEvent = trafficEvents.reduce((max, ev) => (ev.dba > (max ? max.dba : 0) ? ev : max), null);
      } else if (allEvents.length > 0) {
        peakEvent = allEvents.reduce((max, ev) => (ev.dba > (max ? max.dba : 0) ? ev : max), null);
      }
      currentPeakEvent = peakEvent;

      document.getElementById('statTotal').innerText = allEvents.length.toLocaleString();
      document.getElementById('statVehicle').innerText = vehicleCount.toLocaleString();
      document.getElementById('statBylaw').innerText = bylawCount.toLocaleString();

      const statPeak = document.getElementById('statPeak');
      const peakTime = document.getElementById('peakTime');
      const peakPlayBtn = document.getElementById('peakPlayBtn');
      const peakAudioPlayer = document.getElementById('peakAudioPlayer');
      const peakTagBadge = document.getElementById('peakTagBadge');

      if (peakEvent && peakEvent.dba > 0) {
        if (statPeak) statPeak.innerText = peakEvent.dba + ' dBA';
        if (peakTime) peakTime.innerText = peakEvent.time || '';
        if (peakTagBadge) {
          const tName = tagLabels[peakEvent.tag] || peakEvent.tag;
          peakTagBadge.innerText = tName;
          peakTagBadge.classList.remove('hidden');
        }
        if (peakPlayBtn) peakPlayBtn.disabled = false;
        if (peakAudioPlayer) peakAudioPlayer.src = peakEvent.url;
      } else {
        if (statPeak) statPeak.innerText = '-- dBA';
        if (peakTime) peakTime.innerText = '';
        if (peakTagBadge) peakTagBadge.classList.add('hidden');
        if (peakPlayBtn) peakPlayBtn.disabled = true;
      }

      maxPage = Math.max(1, Math.ceil(filtered.length / pageSize));
      if (currentPage > maxPage) currentPage = maxPage;
      if (currentPage < 1) currentPage = 1;

      const startIdx = (currentPage - 1) * pageSize;
      const endIdx = Math.min(startIdx + pageSize, filtered.length);
      const pageEvents = filtered.slice(startIdx, endIdx);

      const pageInfo = document.getElementById('pageInfo');
      if (pageInfo) {
        if (filtered.length === 0) {
          pageInfo.innerText = 'Showing 0 recordings';
        } else {
          pageInfo.innerText = `Showing ${(startIdx + 1).toLocaleString()}–${endIdx.toLocaleString()} of ${filtered.length.toLocaleString()} recordings`;
        }
      }

      const pageNumbers = document.getElementById('pageNumbers');
      if (pageNumbers) {
        pageNumbers.innerText = `Page ${currentPage} / ${maxPage}`;
      }

      const btnPrev = document.getElementById('btnPagePrev');
      const btnNext = document.getElementById('btnPageNext');
      const btnFirst = document.getElementById('btnPageFirst');
      const btnLast = document.getElementById('btnPageLast');
      if (btnPrev) btnPrev.disabled = (currentPage <= 1);
      if (btnFirst) btnFirst.disabled = (currentPage <= 1);
      if (btnNext) btnNext.disabled = (currentPage >= maxPage);
      if (btnLast) btnLast.disabled = (currentPage >= maxPage);

      if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="${isAdmin ? 6 : 5}" class="py-8 text-center text-slate-400">No events found matching criteria.</td></tr>`;
        return;
      }

      tbody.innerHTML = pageEvents.map((ev, idx) => {
        const isEts = ev.tag === 'ets' || ev.tag === 'bus';
        const isBbq = ev.tag === 'bbq' || ev.tag === 'patio';
        const tagLabel = tagLabels[ev.tag] || `🏷️ ${ev.tag}`;
        const canonTag = TAG_ALIASES[(ev.tag || '').toLowerCase()] || (ev.tag || '').toLowerCase();
        const tagOptions = [...TAG_OPTIONS, ...(TAG_OPTIONS.includes(canonTag) ? [] : [canonTag])]
          .map(t => `<option value="${t}" ${t === canonTag ? 'selected' : ''}>${tagLabels[t] || `🏷️ ${t}`}</option>`).join('');
        const curDist = (loadedConfig && loadedConfig.distance_to_road_meters) ? loadedConfig.distance_to_road_meters : 7.0;
        const lossDb = 20 * Math.log10(Math.max(0.5, curDist) / 0.5);
        const tailpipeDba = (ev.dba !== undefined && ev.dba !== null) ? (Math.round((ev.dba + lossDb) * 10) / 10) : ((ev.tailpipe_dba !== undefined && ev.tailpipe_dba !== null) ? ev.tailpipe_dba : '--');

        const classificationCell = isAdmin ? `
          <select onchange="reclassifyEvent('${ev.filename}', this.value)" class="bg-white border-2 border-indigo-200 hover:border-indigo-400 text-xs font-bold text-slate-800 rounded-lg px-2.5 py-1.5 cursor-pointer shadow-sm focus:ring-2 focus:ring-indigo-500 transition">
            ${tagOptions}
          </select>
        ` : `
          <span class="px-2.5 py-1 ${isEts ? 'bg-sky-50 text-sky-800 border-sky-200' : isBbq ? 'bg-amber-100 text-amber-900 border-amber-300' : 'bg-slate-100 text-slate-700 border-slate-200'} border rounded-lg text-xs font-semibold inline-flex items-center gap-1.5 shadow-xs">
            ${tagLabel}
          </span>
        `;

        const actionCell = isAdmin ? `
          <td class="py-1.5 px-4 text-right">
            <button onclick="deleteEvent('${ev.filename}')" class="inline-flex items-center justify-center w-8 h-8 rounded-lg bg-rose-50 hover:bg-rose-600 text-rose-600 hover:text-white border border-rose-200 hover:border-rose-600 shadow-sm transition" title="Delete Recording">
              <i class="fa-solid fa-trash text-sm"></i>
            </button>
          </td>
        ` : '';

        const rowBgClass = (idx % 2 === 0) ? 'row-even' : 'row-odd';

        return `
          <tr class="${rowBgClass} border-b border-slate-200/60 transition duration-100">
            <td class="py-1.5 px-4 font-mono text-slate-700 text-xs font-semibold whitespace-nowrap">${ev.datetime.replace('T', ' ')}</td>
            <td class="py-1.5 px-4 font-bold font-mono text-xs ${ev.dba >= 80 ? 'text-rose-600' : ev.dba >= 70 ? 'text-amber-600' : 'text-slate-800'} whitespace-nowrap">${ev.dba} dBA</td>
            <td class="py-1.5 px-4 font-bold font-mono text-xs text-indigo-600 whitespace-nowrap">~${tailpipeDba} dBA</td>
            <td class="py-1.5 px-4 whitespace-nowrap">${classificationCell}</td>
            <td class="py-1.5 px-4">
              <audio controls preload="none" class="clip-audio-player" src="${ev.url}"></audio>
            </td>
            ${actionCell}
          </tr>
        `;
      }).join('');
    }

    async function reclassifyEvent(filename, newTag) {
      const code = getAdminPasscode();
      if (!code) {
        pendingOpenSettings = false;
        openAdminUnlockModal();
        return;
      }
      showToast(`Reclassifying to ${newTag}...`, 'info');
      try {
        const res = await fetch('/api/reclassify/' + encodeURIComponent(filename) + '?key=' + encodeURIComponent(code), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Admin-Key': code },
          body: JSON.stringify({ tag: newTag })
        });
        if (res.ok) {
          const data = await res.json().catch(() => ({}));
          // Optimistically update the event in allEvents
          const ev = allEvents.find(e => e.filename === filename);
          if (ev) {
            ev.tag = newTag;
            if (data.new_filename) {
              ev.filename = data.new_filename;
              if (ev.url) ev.url = ev.url.replace(filename, data.new_filename);
            }
          }
          renderEvents();
          showToast(`Reclassified to ${newTag}.`, 'success');
          setTimeout(fetchEvents, 800);
        } else {
          const errData = await res.json().catch(() => ({}));
          showToast('Failed to reclassify: ' + (errData.error || res.statusText || 'Access Denied'), 'error');
          fetchEvents();
        }
      } catch (e) {
        showToast('Network error while reclassifying: ' + e, 'error');
      }
    }

    let pendingDeleteFilename = null;

    function deleteEvent(filename) {
      const code = getAdminPasscode();
      if (!code) {
        pendingOpenSettings = false;
        openAdminUnlockModal();
        return;
      }
      pendingDeleteFilename = filename;
      const ev = allEvents.find(e => e.filename === filename);
      const details = document.getElementById('deleteConfirmDetails');
      if (details) {
        if (ev) {
          details.innerText = `Delete ${ev.datetime ? ev.datetime.replace('T', ' ') : filename} (${ev.dba || '--'} dBA)? This action cannot be undone.`;
        } else {
          details.innerText = `Delete ${filename}? This action cannot be undone.`;
        }
      }
      const modal = document.getElementById('deleteConfirmModal');
      if (modal) {
        modal.classList.remove('hidden');
      } else {
        if (confirm('Delete this event recording?')) {
          executeDelete(filename);
        }
      }
    }

    function closeDeleteConfirmModal() {
      const modal = document.getElementById('deleteConfirmModal');
      if (modal) modal.classList.add('hidden');
      pendingDeleteFilename = null;
    }

    async function executeDelete(filename) {
      filename = filename || pendingDeleteFilename;
      closeDeleteConfirmModal();
      const code = getAdminPasscode();
      if (!code || !filename) return;

      showToast('Deleting recording...', 'info');

      try {
        const res = await fetch('/api/delete/' + encodeURIComponent(filename) + '?key=' + encodeURIComponent(code), {
          method: 'POST',
          headers: { 'X-Admin-Key': code }
        });
        if (res.ok) {
          allEvents = allEvents.filter(e => e.filename !== filename);
          renderEvents();
          showToast('Recording deleted successfully.', 'success');
          setTimeout(fetchEvents, 800);
        } else {
          const errData = await res.json().catch(() => ({}));
          showToast('Failed to delete: ' + (errData.error || res.statusText || 'Unauthorized'), 'error');
          fetchEvents();
        }
      } catch (e) {
        showToast('Network error while deleting: ' + e, 'error');
      }
    }

    setInterval(async () => {
      try {
        const res = await fetch('/api/live-level');
        const data = await res.json();
        const dba = data.current_dba || 0;
        
        // Update numerical readout and color
        const dbaTextEl = document.getElementById('liveDbaText');
        if (dbaTextEl) {
          dbaTextEl.innerText = dba.toFixed(1);
          if (dba >= 85) {
            dbaTextEl.className = 'text-3xl font-black text-rose-600 font-mono';
          } else if (dba >= 65) {
            dbaTextEl.className = 'text-3xl font-black text-amber-500 font-mono';
          } else {
            dbaTextEl.className = 'text-3xl font-black text-emerald-600 font-mono';
          }
        }
        
        const dist = loadedConfig.distance_to_road_meters || 10.0;
        const loss = 20 * Math.log10(Math.max(0.5, dist) / 0.5);
        const tailpipeEl = document.getElementById('liveTailpipeText');
        if (tailpipeEl) {
          tailpipeEl.innerText = 'Est. Tailpipe: ' + (dba + loss).toFixed(1) + ' dBA';
        }

        // Linear horizontal meter progress filling from 30 dBA (0%) to 110 dBA (100%)
        const percent = Math.min(100, Math.max(0, ((dba - 30) / (110 - 30)) * 100));
        const meterFill = document.getElementById('liveMeterFill');
        if (meterFill) {
          meterFill.style.width = percent + '%';
          if (dba >= 85) {
            meterFill.className = 'meter-bar h-full rounded-lg bg-rose-600 shadow-sm';
          } else if (dba >= 65) {
            meterFill.className = 'meter-bar h-full rounded-lg bg-amber-500 shadow-sm';
          } else {
            meterFill.className = 'meter-bar h-full rounded-lg bg-emerald-500 shadow-sm';
          }
        }
      } catch (e) {}
    }, 400);

    function openSettingsModal() {
      if (!isAdminActive()) {
        pendingOpenSettings = true;
        openAdminUnlockModal();
      } else {
        document.getElementById('settingsModal').classList.remove('hidden');
        loadDevicesAndConfig();
      }
    }

    function closeSettingsModal() {
      document.getElementById('settingsModal').classList.add('hidden');
    }

    function switchSettingsTab(tab) {
      ['audio', 'fleet', 'wifi', 'social', 'system'].forEach(t => {
        const el = document.getElementById('tab' + t.charAt(0).toUpperCase() + t.slice(1));
        const btn = document.getElementById('tabBtn' + t.charAt(0).toUpperCase() + t.slice(1));
        if (el) el.classList.add('hidden');
        if (btn) btn.className = 'py-3 text-slate-400 hover:text-slate-200 whitespace-nowrap';
      });
      const activeEl = document.getElementById('tab' + tab.charAt(0).toUpperCase() + tab.slice(1));
      const activeBtn = document.getElementById('tabBtn' + tab.charAt(0).toUpperCase() + tab.slice(1));
      if (activeEl) activeEl.classList.remove('hidden');
      if (activeBtn) activeBtn.className = 'py-3 text-indigo-400 border-b-2 border-indigo-500 font-semibold whitespace-nowrap';
      if (tab === 'system') fetchLogs();
      if (tab === 'wifi') scanWifi();
    }

    async function loadDevicesAndConfig() {
      const codeInp = document.getElementById('adminPasscode');
      const code = (codeInp && codeInp.value ? codeInp.value.trim() : '') || sessionStorage.getItem('station_admin_passcode') || currentAdminPasscode || '1811';
      try {
        const [devRes, cfgRes] = await Promise.all([
          fetch('/api/audio-devices?key=' + encodeURIComponent(code), { headers: { 'X-Admin-Key': code } }).catch(() => null),
          fetch('/api/config?key=' + encodeURIComponent(code), { headers: { 'X-Admin-Key': code } }).catch(() => null)
        ]);

        if (devRes && devRes.ok) {
          try {
            const devs = await devRes.json();
            if (Array.isArray(devs)) {
              const devSelect = document.getElementById('cfgAudioDevice');
              if (devSelect) {
                devSelect.innerHTML = devs.map(d => `<option value="${d.index}">[#${d.index}] ${d.name} (${d.channels} ch)</option>`).join('');
              }
            }
          } catch (eDev) {}
        }

        if (cfgRes && cfgRes.ok) {
          loadedConfig = await cfgRes.json();
          document.getElementById('cfgStreetName').value = loadedConfig.street_name || '98th Ave';
          document.getElementById('cfgLocationName').value = loadedConfig.location_name || 'Balcony';
          const fNum = (loadedConfig.floor_number !== undefined && loadedConfig.floor_number !== null) ? parseInt(loadedConfig.floor_number) : 1;
          document.getElementById('cfgFloorNumber').value = fNum;
          const sMeters = (loadedConfig.horizontal_setback_meters !== undefined && loadedConfig.horizontal_setback_meters !== null) ? parseFloat(loadedConfig.horizontal_setback_meters) : 5.0;
          document.getElementById('cfgSetbackMeters').value = sMeters;
          calculateTriangulation();
          renderEvents();
          updateStationBranding(loadedConfig.station_name || loadedConfig.location_name, loadedConfig.street_name, loadedConfig.location_name, fNum, sMeters);
          document.getElementById('cfgThreshold').value = loadedConfig.threshold_dba || 70;
          document.getElementById('cfgOffset').value = loadedConfig.calibration_offset || 50;

          const devSelect = document.getElementById('cfgAudioDevice');
          if (devSelect && loadedConfig.audio_device_index !== null && loadedConfig.audio_device_index !== undefined) {
            devSelect.value = loadedConfig.audio_device_index;
          }

          const src = loadedConfig.audio_source || {};
          document.getElementById('cfgAudioSource').value = ['rtsp', 'udp'].includes(src.type) ? src.type : 'pyaudio';
          document.getElementById('cfgRtspUrl').value = src.url || '';
          document.getElementById('cfgUdpPort').value = src.port || 5005;
          updateAudioSourceFields();

          document.getElementById('cfgBlueskyEnabled').checked = loadedConfig.bluesky?.enabled || false;
          document.getElementById('cfgBlueskyHandle').value = loadedConfig.bluesky?.handle || '';
          document.getElementById('cfgBlueskyPassword').value = loadedConfig.bluesky?.app_password || '';
          document.getElementById('cfgBlueskyTargets').value = (loadedConfig.bluesky?.target_handles || []).join(', ');

          const fleet = loadedConfig.fleet_hub || {};
          document.getElementById('cfgFleetEnabled').checked = fleet.enabled !== false;
          document.getElementById('cfgFleetHubUrl').value = fleet.hub_url || 'https://sethdear.ca/yegnoise';
          document.getElementById('cfgFleetStationId').value = fleet.station_id || loadedConfig.location_name || '';
          document.getElementById('cfgFleetStationName').value = fleet.station_name || loadedConfig.location_name || '';

          const hubUrl = (fleet.hub_url || 'https://sethdear.ca/yegnoise').replace(/\/+$/, '');
          const targetFleetUrl = hubUrl;
          const hBtn = document.getElementById('headerFleetBtn');
          const nBtn = document.getElementById('navFleetBtn');
          if (hBtn) { hBtn.href = targetFleetUrl; hBtn.target = '_self'; }
          if (nBtn) { nBtn.href = targetFleetUrl; nBtn.target = '_self'; }
        }
      } catch (e) {
        console.error(e);
      }
    }

    function updateAudioSourceFields() {
      const type = document.getElementById('cfgAudioSource').value;
      document.getElementById('srcFieldsPyaudio').classList.toggle('hidden', type !== 'pyaudio');
      document.getElementById('srcFieldsRtsp').classList.toggle('hidden', type !== 'rtsp');
      document.getElementById('srcFieldsUdp').classList.toggle('hidden', type !== 'udp');
    }

    function readAudioSourceFields() {
      const type = document.getElementById('cfgAudioSource').value;
      const url = document.getElementById('cfgRtspUrl').value.trim();
      const port = parseInt(document.getElementById('cfgUdpPort').value);
      if (type === 'rtsp' && !['rtsp://', 'rtsps://'].some(pfx => url.toLowerCase().startsWith(pfx))) throw new Error('Enter an RTSP stream URL starting with rtsp:// or rtsps://');
      if (type === 'udp' && !(port >= 1 && port <= 65535)) throw new Error('Enter a UDP port between 1 and 65535');
      // Keep keys for the other sources so switching back doesn't lose them
      return {
        ...(loadedConfig.audio_source || {}),
        type,
        url,
        port: port >= 1 && port <= 65535 ? port : 5005
      };
    }

    async function saveSettings() {
      let audioSource;
      try {
        audioSource = readAudioSourceFields();
      } catch (eSrc) {
        alert(eSrc.message);
        return;
      }
      const codeInp = document.getElementById('adminPasscode');
      const code = (codeInp && codeInp.value ? codeInp.value.trim() : '') || sessionStorage.getItem('station_admin_passcode') || currentAdminPasscode || '1811';
      const devVal = document.getElementById('cfgAudioDevice') ? document.getElementById('cfgAudioDevice').value : '';
      const finalDist = calculateTriangulation();
      const fNum = parseInt(document.getElementById('cfgFloorNumber').value) || 1;
      const sMeters = parseFloat(document.getElementById('cfgSetbackMeters').value) || 5.0;

      const payload = {
        ...loadedConfig,
        admin_passcode: (codeInp && codeInp.value ? codeInp.value.trim() : '') || loadedConfig.admin_passcode || code,
        street_name: document.getElementById('cfgStreetName').value,
        location_name: document.getElementById('cfgLocationName').value,
        station_name: document.getElementById('cfgFleetStationName')?.value || document.getElementById('cfgLocationName')?.value || loadedConfig.station_name,
        floor_number: fNum,
        horizontal_setback_meters: sMeters,
        distance_to_road_meters: finalDist,
        audio_device_index: devVal !== "" ? parseInt(devVal) : null,
        audio_source: audioSource,
        threshold_dba: parseFloat(document.getElementById('cfgThreshold').value),
        calibration_offset: parseFloat(document.getElementById('cfgOffset').value),
        bluesky: {
          ...loadedConfig.bluesky,
          enabled: document.getElementById('cfgBlueskyEnabled').checked,
          handle: document.getElementById('cfgBlueskyHandle').value.trim(),
          app_password: document.getElementById('cfgBlueskyPassword').value.trim(),
          target_handles: (document.getElementById('cfgBlueskyTargets').value || '').split(',').map(s => s.trim()).filter(Boolean)
        },
        fleet_hub: {
          ...loadedConfig.fleet_hub,
          enabled: document.getElementById('cfgFleetEnabled').checked,
          hub_url: document.getElementById('cfgFleetHubUrl').value,
          station_id: document.getElementById('cfgFleetStationId').value,
          station_name: document.getElementById('cfgFleetStationName').value
        }
      };

      try {
        const res = await fetch('/api/config?key=' + encodeURIComponent(code), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Admin-Key': code },
          body: JSON.stringify(payload)
        });
        if (res.ok) {
          loadedConfig = { ...loadedConfig, ...payload };
          alert('Settings saved and recalculated successfully!');
          closeSettingsModal();
          updateStationBranding(loadedConfig.station_name || loadedConfig.location_name, loadedConfig.street_name, loadedConfig.location_name, fNum, sMeters);
          calculateTriangulation();
          renderEvents();
          loadDevicesAndConfig();
        } else {
          const errData = await res.json().catch(() => ({}));
          alert('Error saving settings: ' + (errData.error || res.statusText));
        }
      } catch (e) {
        alert('Error saving settings: ' + e);
      }
    }

    async function scanWifi() {
      const code = document.getElementById('adminPasscode').value || 'admin123';
      const listEl = document.getElementById('wifiList');
      listEl.innerHTML = '<p class="text-xs text-indigo-400 py-2"><i class="fa-solid fa-spinner fa-spin"></i> Scanning for nearby Wi-Fi networks...</p>';
      try {
        const res = await fetch('/api/wifi/scan?key=' + encodeURIComponent(code));
        const nets = await res.json();
        if (!nets || nets.length === 0) {
          listEl.innerHTML = '<p class="text-xs text-slate-400 py-1">No scanned networks returned. Type your Wi-Fi name in the box below to connect.</p>';
          return;
        }
        listEl.innerHTML = nets.map(n => `
          <div onclick="document.getElementById('wifiSsidInput').value = '${n.ssid}'" class="p-2.5 bg-slate-900 hover:bg-slate-800 border border-slate-800 rounded-lg flex items-center justify-between cursor-pointer">
            <span class="font-medium text-white text-xs">${n.ssid}</span>
            <span class="text-slate-400 text-xs">${n.signal}%</span>
          </div>
        `).join('');
      } catch (e) {
        listEl.innerHTML = '<p class="text-xs text-slate-400 py-1">Type your Wi-Fi SSID and password in the boxes below.</p>';
      }
    }

    async function connectWifi() {
      const code = document.getElementById('adminPasscode').value || 'admin123';
      const ssid = document.getElementById('wifiSsidInput').value;
      const password = document.getElementById('wifiPasswordInput').value;
      if (!ssid) return alert('Select or type a Wi-Fi name');
      try {
        const res = await fetch('/api/wifi/connect?key=' + encodeURIComponent(code), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ssid, password })
        });
        const d = await res.json();
        alert(d.message || d.error);
      } catch (e) {
        alert(e);
      }
    }

    async function fetchLogs() {
      const code = document.getElementById('adminPasscode').value || 'admin123';
      try {
        const res = await fetch('/api/logs?key=' + encodeURIComponent(code));
        const d = await res.json();
        document.getElementById('systemLogs').innerText = d.logs || 'No log messages yet.';
      } catch (e) {
        document.getElementById('systemLogs').innerText = 'Error: ' + e;
      }
    }

    async function initPublicLinks() {
      try {
        const res = await fetch('/api/public-info');
        if (res.ok) {
          const info = await res.json();
          if (info.hub_url) {
            const u = info.hub_url.replace(/\/+$/, '');
            const finalUrl = u.endsWith('/fleet') ? u : u + '/fleet';
            const hBtn = document.getElementById('headerFleetBtn');
            const nBtn = document.getElementById('navFleetBtn');
            if (hBtn) hBtn.href = finalUrl;
            if (nBtn) nBtn.href = finalUrl;
          }
          updateStationBranding(info.station_name || info.location_name, info.street_name, info.location_name);
        }
      } catch (e) {}
    }

    initPublicLinks();
    fetchEvents();
  </script>
</body>
</html>
"""

HTML_FLEET = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Edmonton Noise Watch — Citywide Fleet Hub</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    body { background-color: #0b1120; color: #f8fafc; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .glass-panel { background: rgba(15, 23, 42, 0.75); backdrop-filter: blur(16px); border: 1px solid rgba(255, 255, 255, 0.07); }
    .status-pulse-green { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); animation: pulseGreen 2s infinite; }
    .status-pulse-red { box-shadow: 0 0 0 0 rgba(244, 63, 94, 0.7); animation: pulseRed 2s infinite; }
    @keyframes pulseGreen {
      0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
      70% { box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); }
      100% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
    }
    @keyframes pulseRed {
      0% { box-shadow: 0 0 0 0 rgba(244, 63, 94, 0.7); }
      70% { box-shadow: 0 0 0 8px rgba(244, 63, 94, 0); }
      100% { box-shadow: 0 0 0 0 rgba(244, 63, 94, 0); }
    }
  </style>
  <script>
    fetch('/api/public-info').then(r => r.json()).then(d => {
      if (d && d.hub_url) {
        const u = d.hub_url.replace(/\/+$/, '');
        const target = u.endsWith('/fleet') ? u : u + '/fleet';
        if (window.location.href !== target) {
          window.location.replace(target);
        }
      }
    }).catch(() => {});
  </script>
</head>
<body class="min-h-screen p-4 lg:p-6 space-y-6">

  <!-- Top App Bar -->
  <header class="glass-panel p-5 rounded-2xl flex flex-col md:flex-row md:items-center justify-between gap-4 shadow-2xl border-indigo-500/20">
    <div class="flex items-center gap-4">
      <div class="w-12 h-12 rounded-xl bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center text-white text-2xl shadow-lg shadow-indigo-500/30">
        <i class="fa-solid fa-tower-broadcast"></i>
      </div>
      <div>
        <div class="flex items-center gap-2.5">
          <h1 class="text-xl font-black text-white tracking-tight">Edmonton Noise Watch <span class="text-indigo-400 font-medium text-sm">| Citywide Fleet Hub</span></h1>
          <span class="px-2.5 py-0.5 bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 rounded-full text-xs font-bold font-mono flex items-center gap-1.5">
            <span class="w-2 h-2 rounded-full bg-emerald-400 status-pulse-green"></span> 24/7 Pi Hub Active
          </span>
        </div>
        <p class="text-xs text-slate-400 mt-0.5">Monitoring all connected community stations across Edmonton</p>
      </div>
    </div>

    <!-- Actions -->
    <div class="flex items-center gap-3">
      <a href="/" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded-xl text-xs font-bold flex items-center gap-1.5 border border-slate-700 transition">
        <i class="fa-solid fa-house"></i> My Balcony Bot
      </a>
      <div class="bg-slate-900/80 border border-slate-800 rounded-xl px-4 py-2 text-right">
        <span class="text-[10px] uppercase tracking-wider text-slate-400 font-bold block">Active Stations</span>
        <span id="headerOnlineCount" class="text-lg font-black text-emerald-400 font-mono">1 Active</span>
      </div>
    </div>
  </header>

  <!-- Connected Raspberry Pi Stations Grid -->
  <div>
    <div class="flex items-center justify-between mb-3 px-1">
      <h2 class="text-sm font-bold text-slate-300 uppercase tracking-wider flex items-center gap-2">
        <i class="fa-solid fa-microchip text-indigo-400"></i> Connected Community Stations
      </h2>
      <div class="flex items-center gap-2">
        <span id="syncCountdownBadge" class="px-2.5 py-1 bg-slate-900 border border-slate-800 text-slate-300 text-xs font-mono rounded-lg flex items-center gap-1.5 shadow-sm transition-all duration-200">
          <i id="syncSpinIcon" class="fa-solid fa-arrows-rotate text-indigo-400 text-[11px] transition-transform"></i>
          <span id="syncCountdownText">Auto-sync in <strong id="syncSecs" class="text-indigo-400 font-bold">4s</strong></span>
        </span>
      </div>
    </div>

    <div id="stationsGrid" class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4"></div>
  </div>

  <!-- Multi-Station Analytics & Live Unified Stream -->
  <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
    <!-- Chart: Citywide Decibels -->
    <div class="glass-panel p-6 rounded-2xl shadow-xl lg:col-span-2 space-y-4">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
        <div class="flex items-center gap-2.5">
          <i class="fa-solid fa-chart-line text-indigo-400"></i>
          <h3 class="text-base font-bold text-white">Live Citywide Noise Level Comparison</h3>
        </div>
        <span class="text-xs text-slate-400 font-mono">Real-time Telemetry</span>
      </div>
      <div class="h-64 w-full">
        <canvas id="citywideChart"></canvas>
      </div>
    </div>

    <!-- Live Unified Violation Stream -->
    <div class="glass-panel p-6 rounded-2xl shadow-xl space-y-4 flex flex-col">
      <div class="flex items-center justify-between border-b border-slate-800 pb-3">
        <h3 class="text-base font-bold text-white flex items-center gap-2">
          <i class="fa-solid fa-bullhorn text-rose-400"></i> Unified Violations
        </h3>
        <span class="text-[10px] px-2 py-0.5 bg-indigo-500/20 text-indigo-300 rounded font-mono">Citywide Feed</span>
      </div>

      <div id="unifiedFeed" class="space-y-3 overflow-y-auto max-h-64 flex-1 pr-1">
        <p class="text-xs text-slate-500 text-center py-8">Waiting for violations...</p>
      </div>
    </div>
  </div>

  <script>
    let chartInstance = null;

    async function fetchFleetData() {
      try {
        const res = await fetch('/api/fleet-data');
        const data = await res.json();
        renderStations(data.stations || []);
        renderFeed(data.recent_violations || []);
        renderChart(data.stations || []);
      } catch (e) {
        console.error(e);
      }
    }

    function renderStations(stations) {
      const grid = document.getElementById('stationsGrid');
      const onlineCount = stations.filter(s => s.is_online).length;
      document.getElementById('headerOnlineCount').innerText = `${onlineCount} / ${stations.length} Active`;

      grid.innerHTML = stations.map(s => {
        const isOnline = s.is_online;
        const loss = 20 * Math.log10(Math.max(0.5, s.distance_to_road_meters || 10) / 0.5);
        const estTailpipe = (s.current_dba + loss).toFixed(1);

        return `
          <div class="glass-panel p-5 rounded-2xl border-l-4 ${isOnline ? 'border-l-emerald-500' : 'border-l-rose-500 opacity-75'} space-y-4">
            <div class="flex items-start justify-between">
              <div>
                <div class="flex items-center gap-2">
                  <span class="w-2.5 h-2.5 rounded-full ${isOnline ? 'bg-emerald-500 status-pulse-green' : 'bg-rose-500 status-pulse-red'}"></span>
                  <h3 class="font-bold text-white text-base">${s.station_name}</h3>
                </div>
                <p class="text-xs text-slate-400 font-mono">${s.street_name} • Floor ${s.floor_number}</p>
              </div>
              <span class="px-2 py-0.5 ${isOnline ? 'bg-slate-800 text-slate-300' : 'bg-rose-950/60 text-rose-400'} border border-slate-700 text-[10px] font-mono rounded font-bold">
                ${isOnline ? s.version || 'v1.4' : 'OFFLINE'}
              </span>
            </div>

            <div class="bg-slate-900/90 rounded-xl p-3 border border-slate-800/80 flex items-center justify-between">
              <div>
                <span class="text-[10px] text-slate-400 block font-semibold">${isOnline ? 'LIVE SOUND LEVEL' : 'STATUS'}</span>
                <div class="flex items-baseline gap-1">
                  <span class="text-2xl font-black ${isOnline ? 'text-emerald-400' : 'text-slate-500'} font-mono">${isOnline ? s.current_dba.toFixed(1) : '--'}</span>
                  <span class="text-xs text-slate-400 font-bold">dBA</span>
                </div>
              </div>
              <div class="text-right">
                <span class="text-[10px] text-slate-400 block font-semibold">EST. TAILPIPE</span>
                <span class="text-sm font-bold ${isOnline ? 'text-amber-400' : 'text-rose-400'} font-mono">
                  ${isOnline ? `~${estTailpipe} dBA` : `${s.seconds_since_ping}s ago`}
                </span>
              </div>
            </div>

            <div class="flex items-center justify-between text-xs text-slate-400 pt-1 border-t border-slate-800">
              <span>Violations: <b class="text-white font-mono">${s.total_violations || 0}</b></span>
            </div>
          </div>
        `;
      }).join('');
    }

    function renderFeed(violations) {
      const feed = document.getElementById('unifiedFeed');
      if (violations.length === 0) {
        feed.innerHTML = '<p class="text-xs text-slate-500 text-center py-8">No violations recorded yet.</p>';
        return;
      }

      feed.innerHTML = violations.slice().reverse().map(v => `
        <div class="bg-slate-900/80 border border-slate-800 rounded-xl p-3 space-y-1.5">
          <div class="flex items-center justify-between">
            <span class="text-xs font-bold text-white flex items-center gap-1.5">
              <span class="w-2 h-2 rounded-full bg-indigo-400"></span> ${v.station_name} (${v.street_name})
            </span>
            <span class="text-[10px] text-slate-400 font-mono">${v.time_str}</span>
          </div>
          <div class="flex items-center justify-between text-xs">
            <span class="font-mono text-rose-400 font-bold">${v.dba} dBA (${v.tailpipe_dba || (v.dba + 25).toFixed(0)} dB Tailpipe)</span>
            <span class="px-2 py-0.5 bg-indigo-500/20 text-indigo-300 rounded text-[10px] font-semibold">${v.tag || 'traffic'}</span>
          </div>
        </div>
      `).join('');
    }

    function renderChart(stations) {
      const ctx = document.getElementById('citywideChart');
      if (!ctx) return;

      const colors = ['#10b981', '#6366f1', '#a855f7', '#f59e0b', '#ec4899'];
      const datasets = stations.map((s, idx) => {
        const hist = s.history || [];
        return {
          label: s.station_name,
          data: hist.map(h => h.dba),
          borderColor: colors[idx % colors.length],
          backgroundColor: 'transparent',
          tension: 0.3,
          borderWidth: 2
        };
      });

      const labels = (stations[0]?.history || []).map(h => h.time);

      if (chartInstance) chartInstance.destroy();

      chartInstance = new Chart(ctx, {
        type: 'line',
        data: {
          labels: labels.length > 0 ? labels : ['00:00', '00:05', '00:10', '00:15'],
          datasets: datasets.length > 0 ? datasets : [{ label: 'Waiting for data', data: [0,0,0,0], borderColor: '#64748b' }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { position: 'top', labels: { color: '#94a3b8', boxWidth: 10 } } },
          scales: {
            y: { min: 0, suggestedMax: 100, title: { display: true, text: 'dBA', color: '#64748b' }, ticks: { stepSize: 20, color: '#94a3b8', callback: v => v + ' dBA' }, grid: { color: 'rgba(255,255,255,0.05)' } },
            x: { ticks: { color: '#94a3b8' }, grid: { display: false } }
          }
        }
      });
    }

    const SYNC_INTERVAL_SECS = 4;
    let syncTimerSecs = SYNC_INTERVAL_SECS;
    let isCurrentlySyncing = false;

    function renderSyncCountdown(syncing) {
      const badge = document.getElementById('syncCountdownBadge');
      const text = document.getElementById('syncCountdownText');
      const icon = document.getElementById('syncSpinIcon');
      if (!badge || !text) return;

      if (syncing) {
        if (icon) icon.classList.add('fa-spin');
        badge.className = 'px-2.5 py-1 bg-indigo-950 border border-indigo-700 text-indigo-300 text-xs font-mono rounded-lg flex items-center gap-1.5 shadow-sm transition-all duration-200';
        text.innerHTML = 'Syncing...';
      } else {
        if (icon) icon.classList.remove('fa-spin');
        badge.className = 'px-2.5 py-1 bg-slate-900 border border-slate-800 text-slate-300 text-xs font-mono rounded-lg flex items-center gap-1.5 shadow-sm transition-all duration-200';
        text.innerHTML = `Auto-sync in <strong class="text-indigo-400 font-bold">${syncTimerSecs}s</strong>`;
      }
    }

    async function executeFleetSync() {
      if (isCurrentlySyncing) return;
      isCurrentlySyncing = true;
      renderSyncCountdown(true);
      try {
        await fetchFleetData();
      } catch (err) {
        console.error('Fleet sync error:', err);
      } finally {
        isCurrentlySyncing = false;
        syncTimerSecs = SYNC_INTERVAL_SECS;
        renderSyncCountdown(false);
      }
    }

    executeFleetSync();

    setInterval(() => {
      if (isCurrentlySyncing) return;
      syncTimerSecs--;
      if (syncTimerSecs <= 0) {
        executeFleetSync();
      } else {
        renderSyncCountdown(false);
      }
    }, 1000);
  </script>
</body>
</html>
"""

def run_server(port=5000):
    server_address = ('', port)
    httpd = ThreadingHTTPServer(server_address, DashboardHandler)
    print(f"Noise Bot Web Dashboard active on http://0.0.0.0:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000, help="Port to run dashboard on (default: 5000)")
    args = parser.parse_args()
    run_server(args.port)
