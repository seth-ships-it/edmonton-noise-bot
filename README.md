# 🔊 Edmonton Noise Bot (v1.4)

An automated, open-source acoustic monitoring station designed for Raspberry Pi to detect, classify, log, and report excessive vehicle exhaust noise in Edmonton corridors.

Live network map and community telemetry: **[Edmonton Noise Watch](https://sethdear.ca/yegnoise/)**

---

## ✨ Features in v1.4
* **Calibrated Acoustic A-Weighting (dBA):** Accurate digital bilinear A-weighting filter powered by SciPy and NumPy.
* **Audio Event Archiving:** Pre-trigger rolling audio buffer and post-trigger WAV recording with full web playback.
* **Heuristic Audio Classification:** FFT spectral centroid and energy distribution analysis to distinguish exhaust rumble from wind, rain, and sirens.
* **Central Fleet Hub Telemetry:** Real-time heartbeat, violation logging, and remote live-level streaming to the central hub.
* **IoT Auto-Provisioner Hotspot:** If Wi-Fi is lost or unplugged, the Pi automatically broadcasts Edmonton-Noise-Bot-Setup for zero-friction browser setup.
* **Turnkey Web Dashboard (:5000):** Full mobile-responsive UI with live decibel meters, audio device selection, distance setback calculation, and alert configuration.
* **Multi-Platform Alerts:** Push notifications to Bluesky, Discord webhooks, and email.
* **Containerized or Native Systemd:** Runs natively on Raspberry Pi OS Bookworm or via Docker Compose with automated Watchtower updates.

---

> **Network audio:** besides a USB mic, the detector can read an IP camera's RTSP audio or an ESP32 microphone over UDP. See [noise-bot-friend/README.md → Network audio sources](noise-bot-friend/README.md#-network-audio-sources-optional).

## 🚀 Quick Start

### 1. Requirements
* **Hardware:** Raspberry Pi (Pi 3B+, Pi 4, Pi 5, or Pi Zero 2W) running Raspberry Pi OS (Bookworm, 64-bit recommended).
* **Audio:** Any standard omnidirectional USB microphone (e.g. Mini USB microphone or USB lavalier).
* **Power & Mount:** 5V USB-C/Micro-USB power supply, outdoor weatherproof junction box or balcony mount.

### 2. Installation

```bash
# Clone the repository
git clone https://github.com/seth-ships-it/edmonton-noise-bot.git
cd edmonton-noise-bot/noise-bot-friend

# Run automated installer
chmod +x install.sh
./install.sh
```

Open http://<pi-ip>:5000 or http://noise-bot.local:5000 in your browser. Default admin passcode is 1811.

---

## 📁 Repository Structure

```text
├── .github/workflows/     # GitHub Actions CI/CD for automated multi-arch Docker builds
└── noise-bot-friend/      # Main station software
    ├── audio_classifier.py    # Frequency domain classification
    ├── dashboard.py           # Flask/REST API & responsive web dashboard (v1.4)
    ├── docker-compose.yml     # Containerized deployment spec
    ├── Dockerfile             # Multi-arch container image
    ├── factory_reset.sh       # Hardware factory reset tool
    ├── install.sh             # Turnkey installer & provisioner setup
    ├── noise_detector.py      # Core audio sampling, dBA filtering & trigger engine
    ├── notifier.py            # Bluesky, Discord, and webhook dispatch
    ├── rtsp_source.py         # IP camera audio input (RTSP via ffmpeg)
    ├── udp_source.py          # ESP32 microphone input (UDP PCM stream)
    ├── calibrated_wav.py      # Calibrated float WAV writer for network sources
    ├── run.py                 # Multi-process supervisor
    └── provisioner/           # IoT Wi-Fi captive portal auto-provisioner
```

---

## 🤝 Contributing
Contributions from audio engineers, developers, and data scientists are welcome! Please open an issue or pull request against the main branch.

---

## 📄 License
MIT License. Community-driven and built for civic accountability.
