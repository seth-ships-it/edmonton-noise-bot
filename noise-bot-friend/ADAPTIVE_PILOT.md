# Adaptive detection: first shadow pilot

This release adds an optional learner beside the current recorder. It starts off
on new installations. `shadow` observes the same mono A-weighted digital samples
and stores comparison results; the existing threshold, eight-second recording
policy and notification cooldown still control public clips. Adaptive capture is
deliberately unavailable in this release, including through the configuration API.

## What it learns

The background is the 20th percentile of valid one-second energy levels over the
recent 15 minutes. Day (06–18), evening (18–23), and night (23–06) follow
`America/Edmonton`, including daylight saving changes. Passages start about 3 dB
above background and end after one second near background, capped at 30 seconds.
Screened heuristic vehicle/bus passages build separate typical-traffic medians.
Sound classifications and weather screening are approximate and require review.

Each period needs 7,200 valid seconds and 50 screened passages, with at least one
minute on each of two dates, before comparisons become ready. The proposed
threshold is `max(background + 10, typical traffic + 6)`. Sustained excess for
0.5 seconds or a short peak another 8 dB higher becomes a shadow capture candidate.
The references and calibration revision are frozen at event start. A candidate
cannot increase its own trigger. Drift is limited to 1 dB/day and 3 dB from the
initial anchor; larger observed shifts flag changed conditions for review.

The measurements use energy averages over exact 125 ms windows. Calibration
offsets affect the approximate dBA display; floor and road setback remain site
metadata. Neither enters the relative decision. A phone comparison is still only
an approximate absolute calibration. Identical USB product names cannot identify
physical microphone swaps; use the relearn control after swapping a mic or moving
the station. Observed hardware gain, input identity, channels, sample rate and
timezone changes also invalidate the saved learner.

## Report and storage

Open `/adaptive` on the Pi dashboard and enter its configured station admin code.
The report includes learning coverage, background/traffic references, quality
flags, queue drops, candidate comparisons and the latest 100 summaries. Playable
private samples are served only through the authenticated adaptive endpoint.
Public upload scans look only in `recordings/`.

Candidate counts refer to segmented passages, not unique confirmed vehicles.
“Fixed” indicates overlap with an actual legacy recording, including its tail.
The separately reported annotated-public-clip total need not equal the number of
fixed-overlapping passages. Counts include learning revisions and should not be
compared as a controlled experiment across microphone or policy changes.

Storage under `adaptive-data/` is bounded:

- One-second features and at most 10,000 detailed candidate/fixed summaries:
  seven days, with a 256 MiB SQLite page limit.
- Hourly coverage: 90 days. Reports show the latest 168 hours.
- Private WAV samples: 128 MiB / seven days, up to 20 disagreement and five other
  comparison samples per local period/date. Summaries continue after sample caps.
- A 256 MiB free-space reserve suspends optional writes. Queue pressure drops
  optional analysis; the existing recorder retains its own audio stream.
- Checkpoints save every minute and on orderly shutdown; a hard power loss can
  discard up to a minute of learning. Reports/transactions update about every ten
  seconds. Corrupt or incompatible state starts fresh learning.

`audio_gaps` records timing discontinuities inferred from capture arrival times,
including delayed processing; it is not a hardware measurement of lost samples.
Those periods are excluded from learning. An SD-card/worker error reports degraded
status and leaves fixed recording active. Resolve the error and toggle off/shadow
to restart the worker. Analysis never opens a second microphone stream.

New public WAVs receive `.event.json` sidecars containing event identity, UTC
times, raw peak/Leq, frozen references, relative excess, calibration revision,
quality and classification context. Upload retries reuse these values. Retagging
keeps event identity and records a reviewed label; deletion removes the sidecar.
Older WAVs show unavailable relative values. Incomplete analysis is explicitly
flagged, and private review clips are never uploaded or notified.

## Native pilot deployment

`adaptive-manifest.json` covers the complete changed module set. Copy those files,
the manifest and `deploy_adaptive.py` into a separate staging directory. Use the
station's existing trusted SSH connection and existing Python environment.

```sh
# From the staging directory; dry run is the default.
/home/sethpi/edmonton_noise_bot/venv/bin/python3 deploy_adaptive.py \
  --root /home/sethpi/edmonton_noise_bot --station-id noise-bot-cloverdale

# After checks pass, append --apply. Backups contain private configuration;
# they stay on the Pi in an owner-only directory. Never publish them.
# To return to the prior installation, use the printed backup path:
/home/sethpi/edmonton_noise_bot/venv/bin/python3 deploy_adaptive.py \
  --root /home/sethpi/edmonton_noise_bot --station-id noise-bot-cloverdale \
  --rollback /home/sethpi/adaptive-backup-YYYYMMDDTHHMMSSZ
```

The helper validates station identity, service working directories, source hashes,
disk reserve and current Pi power flags. It backs up changed modules/settings,
merges only shadow settings, restarts the two native services, checks fresh audio
and restores the previous version on failure. Rollback preserves recordings,
learning evidence and unrelated subsequent configuration changes. The helper is
restricted to Cloverdale's native systemd layout; no fleet-wide deployment occurs.

The staged central-hub patch is `integration/adaptive-hub.patch`; it requires
`event_metadata.py` beside the hub server and a normal hub restart. It preserves
upload sidecars and heartbeat learning status and deduplicates by station/event.
Its fixtures exercise the real upload handler without external requests. The
private pilot report remains on the Pi; aggregate public reporting is future work.
The patch has zero context to exclude unrelated private server configuration;
inspect the target version and apply with `git apply --unidiff-zero` from the hub
server directory when integrating it into a hub checkout.

Container packaging persists `adaptive-data/` and mounts every new dependency.
`docker-compose.adaptive-pilot.yml` selects a locally built versioned image and
disables Watchtower updates for that container. Build the pilot image before
using that override. The current validation/deployment is native systemd only;
container rollout still needs its own lifecycle and audio-device verification.

## Validation and activation gates

Run `python -m unittest discover -s tests -p 'test_*.py'` from the application
directory. Install normal requirements (including `tzdata` on Windows). Tests
cover gaps, invalid data, energy aggregation, readiness, drift, resets, persistence,
storage pressure, private endpoint access, retry metadata and byte-identical
legacy WAVs despite different shadow decisions.

`python adaptive_benchmark.py` replays two minutes of synthetic sound with a full
bounded model in an isolated temporary directory. It neither opens the microphone
nor changes station configuration. Cloverdale's Pi 3 B+ measured 8.7% of one core
and 18.3 MiB incremental peak RSS in this replay; synthetic timing continuity is
not evidence of 24-hour live capture reliability.

Before an adaptive-capture release: collect at least three complete days/nights
(preferably a week), review at least 20 clearly loud and 40 ordinary/non-target
examples, investigate every loud example lost relative to fixed capture, and
evaluate the proposed 90% loud-example capture and 30% nuisance-reduction gates
only when the reviewed sample supports them. Verify stable power, unchanged gain
and calibration, bounded storage, and 24 hours without increased unexplained
capture gaps. Sparse periods stay in learning. Readiness never activates capture.

During the first live check on October 3, the Pi reported current undervoltage /
throttling and capture timing discontinuities. Rollback to the original detector
passed. The pilot remains off pending a power check. The central-hub restart was
blocked by automatic approval review; its tested patch remains staged.
