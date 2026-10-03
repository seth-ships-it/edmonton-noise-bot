"""Report Wi-Fi changes without sending Wi-Fi names or passwords to the hub."""

import hashlib
import os
import re
import subprocess
import threading
import time

_CACHE = {"checked_at": None, "ssid": None}
_LOCK = threading.Lock()


def wifi_network_id(station_id):
    with _LOCK:
        now = time.monotonic()
        checked = _CACHE["checked_at"]
        if checked is None or now - checked >= 10:
            ssid = None
            try:
                result = subprocess.run(
                    ["nmcli", "-t", "--escape", "yes", "-f", "IN-USE,SSID",
                     "device", "wifi", "list", "--rescan", "no"],
                    capture_output=True, text=True, timeout=2,
                    env={**os.environ, "LC_ALL": "C"},
                )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        active, separator, encoded = line.partition(":")
                        if separator and active == "*":
                            candidate = re.sub(r"\\(.)", r"\1", encoded)
                            if candidate and candidate != "Edmonton-Noise-Bot-Setup":
                                ssid = candidate
                                break
            except (OSError, subprocess.SubprocessError):
                pass
            _CACHE.update(checked_at=now, ssid=ssid)
        ssid = _CACHE["ssid"]
    if ssid is None:
        return None
    return "sha256:" + hashlib.sha256((str(station_id) + "\0" + ssid).encode("utf-8")).hexdigest()
