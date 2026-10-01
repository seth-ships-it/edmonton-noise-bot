# Optional station settings update — 1 October 2026

Dragging Horizontal Setback and selecting **Save & Apply** now saves the requested
distance on the Pi. The station acknowledges a hub update only after writing it
to disk. Editing placement preserves microphone calibration, credentials, and
other settings. The floor/setback geometry also reloads in installations that run
the detector directly through systemd.

This branch is an optional source update for self-hosted stations, including
Downtown (109 Street). It does not install itself. No container image is published
by this change. A previous image's Watchtower configuration remains in effect.

## Review

Branch: `fix/persistent-station-settings` in
[seth-ships-it/edmonton-noise-bot](https://github.com/seth-ships-it/edmonton-noise-bot).

The station changes are limited to `noise_detector.py`, `dashboard.py`,
`notifier.py`, the new `config_store.py`, two Compose mounts, and regression tests.
The shared Edmonton hub has the corresponding persistent delivery queue.
This branch does not include unrelated recent station or hub features.

Clone this branch into a **separate review directory**, then inspect its diff
against `6a2e02c`. Do not clone over the running installation or replace its
`config.json` with the repository's example. If you maintain custom source changes,
merge this diff into those files before deploying.

## Install when ready

1. Identify the running station's app directory and whether it uses Docker or
   systemd. Stop its detector/dashboard service or its `noise-bot` container for
   the file update. This briefly pauses recording.
2. Make a private backup of the existing `config.json`, `noise_detector.py`,
   `dashboard.py`, `notifier.py`, and Compose file if used. Also back up
   `config_store.py` if one already exists. Keep the backup outside any public
   repository; configuration can contain passwords. Record the existing calibration
   offset and setback so they can be checked afterwards.
3. Copy the four reviewed Python files (`noise_detector.py`, `dashboard.py`,
   `notifier.py`, `config_store.py`) into that same app directory. Keep the station's
   own configuration, recordings, microphone settings, and station ID.
4. For Docker, retain your existing Compose customizations and add these mounts
   under `services.noise-bot.volumes` alongside the detector/dashboard mounts:

   ```yaml
   - ./config_store.py:/app/config_store.py
   - ./notifier.py:/app/notifier.py
   ```

   Recreate just that service from its existing image:

   ```bash
   docker compose up -d --no-deps --force-recreate noise-bot
   ```

   For systemd, start the services you stopped: commonly `noise-bot.service`, or
   the separate `noise-detector.service` and `noise-dashboard.service` pair.
5. Confirm live readings resume. Drag Horizontal Setback, choose **Save & Apply**,
   wait for the Pi to report, and reopen settings. Verify the distance survives a
   page reload and the calibration offset still matches the value you backed up.
   A placement change may require renewed approval on the shared hub.

`config_store.py` must accompany the updated Python files; an old Docker image
cannot import the new helper without the mount above. Regular config files are
replaced atomically. Linux single-file bind mounts use a locked in-place write
because a mount point cannot be renamed; this has the same power-loss limitation
as an ordinary in-place configuration save.

## Roll back

Stop the same station service/container, restore the backed-up source and Compose
files, and start/recreate that service. Restore the saved configuration only if you
also intend to revert settings changed since the backup. No audio files need to be
removed. On the shared hub, set the desired geometry back too, so an outstanding
queued request is not reapplied after a later update.

## Validation

Run the station tests without audio hardware:

```bash
python3 -m unittest discover -s tests -p test_config_delivery.py
```

Eight station regression tests cover calibration/credential preservation, invalid
placement, repeat delivery, heartbeat acknowledgement, Docker mount writes,
write failures, and coordinated readers. The local project also passes eleven
hub/station settings tests and six recording timestamp tests. A real Linux bind
mount was tested in an isolated mount namespace on a Pi. Docker container startup
has not been exercised on Evan's installation.

Live checks on Parkallen, Pleasantview, and Cloverdale confirmed the hub/Pi update
round trip. Parkallen's 35 m setting survived saving and reopening the browser;
all three phone calibration offsets were preserved. Whyte still needs remote
access before its station software can be updated.
