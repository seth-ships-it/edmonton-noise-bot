# 🔊 Edmonton Traffic Noise Monitor (Docker Edition)

An automated acoustic monitoring station designed for Raspberry Pi to detect, classify, log, and alert on excessive vehicle exhaust noise.

---

## ✨ Features
* **Containerized Deployment:** Powered by Docker Compose with `/dev/snd` pass-through and host networking.
* **Turnkey Web Settings (`/settings`):** Choose the audio source (USB microphone, IP camera RTSP stream or ESP32 Wi-Fi microphone), change input devices, view live decibel calibration meters, configure Wi-Fi, and manage social alerts directly in the browser.
* **Acoustic A-Weighting (dBA):** Accurate digital bilinear A-weighting filter powered by SciPy.
* **Audio Classifier:** Spectral band analysis and crest-factor filtering to distinguish vehicle roar from rain, wind, and thunder.
* **Multi-Platform Alerts:** Optional push notifications to Bluesky, Twitter/X, Discord webhooks, and Email.
* **Audio Event Archiving:** Pre-trigger rolling buffer and post-trigger WAV recording with web playback.

---

## 🚀 Quick Start

```bash
# Clone or extract archive
cd noise-bot-friend

# Run automated installer
chmod +x install.sh
./install.sh
```

Open `http://localhost:5000` (or `http://<pi-ip>:5000`) in your browser to access the dashboard.

## 📡 Network audio sources (optional)

Out of the box the detector listens to a USB microphone through PyAudio. It can instead take audio from the network, which is useful when the bot runs on a server with no sound card (e.g. a NAS or Unraid box) or when a better-placed microphone already exists:

| `audio_source.type` | Input | Bandwidth | Best for |
|---|---|---|---|
| `pyaudio` *(default)* | USB microphone | full (to 24 kHz) | Raspberry Pi with a USB mic: the original setup |
| `rtsp` | Audio track of an IP camera stream, decoded by `ffmpeg` | often only 8 kHz (see below) | Reusing a camera that already faces the street |
| `udp` | ESP32 + I2S MEMS microphone streaming raw PCM over Wi-Fi | full (to 24 kHz) | Accurate measurement with a mic placed where you want it |

Everything downstream (A-weighting, threshold, recording, classifier, notifier, dashboard, fleet hub) is identical for all three. If `audio_source` is missing or `type` is `pyaudio`, behaviour is unchanged from upstream.

**Choosing in the dashboard:** open **Settings** (enter the admin passcode first), pick the source under **Audio Source** and fill in its fields: the device list, the RTSP stream URL, or the UDP port. Saving restarts the detector on the new source within a few seconds. Settings for the sources you're not using are kept, so you can switch back without re-entering the camera URL. Everything below can also be set by hand in `config.json` under `audio_source`.

**Calibration is always `calibration_offset`** (Settings → Calibration Offset), whatever the source. Re-calibrate when you switch sources, because each microphone needs its own offset (see [Calibration](#calibration)).

### Choosing a source

- **Measurement accuracy:** `udp` with a MEMS mic (fixed gain, no AGC, flat response) or a good USB mic. Many IP cameras capture audio at 16 kHz, which removes everything above 8 kHz. dBA is barely affected (traffic energy is mostly low-frequency), but the classifier's 2.5–12 kHz "weather" band loses most of its range, so rain and wind tend to be tagged as traffic.
- **Least hardware:** `rtsp`, if a camera already faces the street.
- **Whatever you choose, check for automatic gain control.** A source that flattens loud sounds makes dBA readings meaningless (see [Calibration](#calibration)).

---

### RTSP camera audio

#### 1. Find the stream URL

| Camera / NVR | Typical URL |
|---|---|
| UniFi Protect | Enable RTSP on a channel (camera → Settings → Advanced → RTSP), then use `rtsps://<console-ip>:7441/<alias>?enableSrtp` (or `rtsp://<console-ip>:7447/<alias>`). The alias is a secret token. |
| Reolink | `rtsp://<user>:<pass>@<camera-ip>:554/h264Preview_01_main` |
| Hikvision | `rtsp://<user>:<pass>@<camera-ip>:554/Streaming/Channels/101` |
| Dahua / Amcrest | `rtsp://<user>:<pass>@<camera-ip>:554/cam/realmonitor?channel=1&subtype=0` |
| Anything else | Check the camera's docs, or try it in VLC: *Media → Open Network Stream* |

Make sure audio (the microphone) is enabled on the camera and included in the stream.

#### 2. Check what the camera actually delivers

```bash
ffprobe -v error -rtsp_transport tcp -select_streams a \
  -show_entries stream=codec_name,sample_rate,channels -of compact "<rtsp-url>"
```

The bot always uses the **first** audio track and resamples it to 48 kHz mono. Some cameras offer several tracks (e.g. AAC at 16 kHz *and* Opus at 48 kHz). A 48 kHz track can still be upsampled from a 16 kHz microphone. To see the real bandwidth, record a few seconds and look at a spectrum or spectrogram (Audacity: *Analyze → Plot Spectrum*). A brick-wall cutoff at 8 kHz means a 16 kHz mic.

#### 3. Configure

In **Settings → Audio Source**, choose **IP Camera (RTSP stream)** and paste the URL. Or in `config.json`:

```json
"audio_source": {
  "type": "rtsp",
  "url": "rtsps://<console-ip>:7441/<alias>?enableSrtp"
}
```

The URL usually contains credentials or a token. Keep it only in `config.json`, never commit it. The bot redacts the path in its own log lines (`rtsps://host:7441/<redacted>`).

#### 4. Camera settings that matter

- **Turn off noise reduction / noise suppression** if the camera has it. It gates quiet passages and colours the spectrum.
- **Mic volume / sensitivity changes the calibration.** On a UniFi G4, 90 % → 100 % added 6 dB. Re-calibrate after any change, and remember that more gain means less headroom: loud events clip sooner.
- Moving the camera also shifts calibration by a few dB (nearby walls and glass reflect sound).

#### How it behaves

- `ffmpeg -rtsp_transport tcp -timeout 5000000 -i <url> -map 0:a:0 -vn -ac 1 -ar 48000 -f s16le -`
- If the stream stalls (5 s I/O timeout) or ffmpeg exits, the gap is filled with silence at real-time pace and ffmpeg is restarted with exponential backoff (1 s, 2 s, 4 s … up to 30 s; reset after a minute of healthy streaming). The A-weighting filter state stays continuous.
- ffmpeg's error output is logged with the URL redacted.
- Expect about 1 s of latency compared with the real world. The 2 s pre-trigger buffer absorbs it.

---

### ESP32 UDP microphone

An ESP32 reads an I2S MEMS microphone and sends raw PCM to the bot over UDP. Nothing is compressed, so the bot gets the same samples the microphone produced.

#### Hardware

- **ESP32-S3** (any board with Wi-Fi and I2S; S3 recommended for headroom).
- **I2S MEMS microphone**, e.g. **INMP441** (24-bit, −26 dBFS sensitivity at 94 dB SPL, flat 60 Hz–15 kHz, no AGC). Boards with an audio ADC such as the ES7210 also work if the ADC's automatic level control is off and its gain is fixed.

Example INMP441 wiring:

| INMP441 | ESP32-S3 |
|---|---|
| VDD | 3V3 |
| GND | GND |
| L/R | GND (left slot) |
| SCK | GPIO4 |
| WS | GPIO5 |
| SD | GPIO6 |

Avoid strapping pins (GPIO0/45/46 on the S3), the native-USB pins (19/20) and octal-PSRAM pins (35–37). Solder the header: a pressed-in header often gives no signal.

#### Firmware requirements

Any firmware works if it:

1. Captures **48 kHz** audio and sends **16-bit signed little-endian mono** samples, taking the **top 16 bits** of the mic's 24-bit word, with **no gain, filtering or AGC**. Calibration is then a single fixed offset.
2. Sends **one packet every 10 ms (480 samples)** to `<bot-ip>:5005` (UDP), using the packet format below.
3. Increments the sequence number for **every** packet period, even when a send fails (e.g. Wi-Fi briefly down), so the receiver can tell lost audio from late audio.
4. Turns Wi-Fi power saving off (it adds latency spikes) and mutes the first ~300 ms after I2S starts (MEMS power-up transients can otherwise trigger an event at every boot).

#### Packet format (little-endian)

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 2 | magic | ASCII `"NB"` |
| 2 | 1 | version | `1`, `2` or `3` |
| 3 | 1 | flags | bit 0 = wind detected (informational) |
| 4 | 4 | sequence | `uint32`, +1 per 10 ms period |
| 8 | 2 | calibration | `int16`, **v2+**: the board's own offset in centi-dB (`12270` = 122.7 dB), `0` = none. Informational, not used by the detector. |
| 10 | 1 | channels | **v3 only**: `1` or `2` |
| 11 | 1 | reserved | **v3 only** |
| … | … | samples | `int16` PCM; stereo is interleaved |

| Version | Header | Payload | Packet |
|---|---|---|---|
| 1 | 8 B | 480 mono samples | 968 B |
| 2 | 10 B | 480 mono samples | 970 B |
| 3 | 12 B | 240 frames per 5 ms, mono or interleaved stereo | ≤ 972 B |

Mono at 48 kHz is about 0.8 Mbit/s. Stereo doubles that and needs a strong Wi-Fi link. The detector always uses channel 1.

#### Configure

In **Settings → Audio Source**, choose **ESP32 Microphone (UDP)** and set the port (default 5005). Or in `config.json`:

```json
"audio_source": {
  "type": "udp",
  "port": 5005
}
```

Calibration comes from `calibration_offset`, as for every other source. The calibration field in the packets is ignored by the detector. If your firmware keeps its own offset for on-device readings (LEDs, Home Assistant sensors), the two are independent: after re-calibrating, set the same value on the board and in the bot. The detector logs the board's value when the stream starts (`ESP32 reports its own calibration offset: … (informational)`), which makes a mismatch easy to spot.

#### Networking

- The bot listens on **UDP 5005** on all interfaces.
- With the supplied `docker-compose.yml` (`network_mode: host`), point the ESP32 at the **host's** IP. With bridge networking, publish the port (`ports: ["5005:5005/udp"]`). On a macvlan/ipvlan network, use the container's own IP.
- If the ESP32 sits on a separate VLAN (e.g. IoT), allow UDP from it to the bot.
- One sender per port. Packets from a second board would interleave and look like constant resyncs.

#### How the receiver behaves (`udp_source.py`)

- Reorders packets within a 3-packet window. A missing packet is declared lost and replaced with silence once three newer ones have arrived.
- Pacing follows the **ESP32's sample clock** (packet arrival), so there's no drift between the board and the server.
- Late packets are waited for up to 1 s, so Wi-Fi delivering audio late in a burst (common on busy 2.4 GHz channels) is played rather than replaced by silence. During a real outage (no packets for 1 s), it emits silence at real-time pace, so the meter drops to 0 and recordings don't stall. It resumes cleanly when packets return.
- A sequence number far behind the current one (> 1000 packets) means the board rebooted, and it resyncs automatically. Gaps over 50 packets mid-stream skip ahead instead of filling.

#### Testing without the bot

`udp_source.py` doubles as a live meter. Stop the bot first, because only one process can bind the port:

```bash
python3 udp_source.py --port 5005 --seconds 30 --wav test.wav
```

Each second it prints RMS/peak dBFS plus received / lost / late / silence / resync counts. Steady `rx 100` with no losses means the link is healthy.

---

### Calibration

dBA = 20·log10(RMS of the A-weighted signal / 32768) + **offset**. The offset is the only calibration value and depends on the microphone, its gain, the enclosure and its position.

1. Put a reference meter **right beside the microphone**. A phone app such as *Decibel X* works as a rough reference (±2–3 dB): set it to **dBA** and **Fast/RMS**. Not "ITU-R 468" or C-weighting, which read 5–10 dB higher on traffic.
2. Record 30–60 s on the reference while the bot runs, ideally with steady sound (traffic hum, a fan) plus a few louder passes.
3. Compare **second by second**, with both clocks aligned. A single spot reading is useless at a street, where levels swing ±5 dB within seconds. Use only steady seconds, because short impulsive sounds don't line up between devices.
4. New offset = old offset + (reference − bot). Set it in `calibration_offset` (Settings → Calibration Offset), whatever the source.
5. **Check for AGC:** play or make sounds at clearly different levels (quiet → clap → louder clap → shout). The bot should rise in step with the reference. A slope near 1.0 is good; readings that flatten mean AGC, and the source shouldn't be trusted.
6. Re-calibrate whenever the gain, position or enclosure changes.

A wall or balcony right behind the mic reads about +3 dB louder than open air (the usual façade effect). That's normal for balcony stations, but worth knowing when comparing stations.

### Calibrated recordings

Event recordings, from every source, are saved as **32-bit float WAV**, scaled by one fixed gain so that **0 dBFS (A-weighted RMS) = 100 dB(A)** (`calibrated_wav_reference_db`):

- Quiet events become audible: a 77 dBA car plays at about −23 dBFS instead of about −45 dBFS raw.
- Relative loudness is kept: a 90 dBA event is still 13 dB louder in its file than a 77 dBA one. No per-file normalisation is applied.
- Float can't clip in the file. Events above ~100 dB(A) exceed 0 dBFS, which the file stores correctly but some players clip on playback.
- The calibration is written to the WAV's `LIST/INFO` comment (`Calibrated: 0 dBFS = 100.0 dB(A) …`).
- The gain comes from `calibration_offset`, so an uncalibrated microphone gives consistently scaled but not truly calibrated files. Calibrate first (see above).
- Set `"calibrated_wav": false` in `audio_source` for raw 16-bit files, e.g. if something downstream expects 16-bit PCM.

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| Meter stuck at **0.0 dB(A)** | The offset is far too low (readings are clamped at 0), or no audio is arriving. Check the log. |
| `Waiting for ESP32 stream, emitting silence` | No UDP packets: wrong target IP/port on the board, a firewall/VLAN rule, or Docker networking (see above). |
| `No audio packets for …s` repeating | Wi-Fi trouble: a weak signal, or a congested 2.4 GHz channel (check the access point's retry rate and channel utilisation, and try a quieter channel or a closer AP). Use mono rather than stereo. |
| `Seq jumped back …, ESP32 restarted` | The board rebooted. Harmless once; if it repeats, check power and the board's watchdog logs. |
| `RTSP audio: short read … reconnecting` repeating | The camera or NVR is dropping the stream. Check the URL, check that audio is enabled, and try the console/NVR IP rather than the camera's. |
| Settings shows "USB Microphone (hw:1,0)" | Placeholder when the container has no sound card. The device list is ignored for `rtsp`/`udp`. |
| Readings suddenly ~6 dB different | Mic gain / volume changed on the camera or board. Re-calibrate. |
| Everything tagged as traffic/vehicle | Expected with an 8 kHz-limited source (see *Choosing a source*). A full-bandwidth source helps. |

