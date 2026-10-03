# Verified local recording retention

The hub keeps the archive. Each Pi keeps a rolling local cache. Cleanup runs on
the hub machine over the station's existing pinned SSH connection, verifying
the actual archived bytes before authorizing deletion on the Pi. This avoids
trusting a playback proxy, fallback audio, an HTTP 200 response, or a historical
uploaded-filenames list as proof of a durable copy.

Default policy for an explicitly enabled station:

- Keep local recordings for up to seven days.
- Remove oldest verified copies sooner when local recordings exceed 2 GiB or
  free space is below 2 GiB.
- Always protect the most recent hour, active temporary files, changed files,
  unverified recordings, and associated metadata that has not been archived.
- Never delete a hub archive file or overwrite a conflicting existing copy.
- When the hub/station connection fails, retain local recordings and retry on
  the next scheduled run. A long outage can still exhaust local storage; this
  policy does not destroy the only copy to make space.

Only Cloverdale is enrolled initially. Other stations require an explicit entry
and pinned host key in the hub's private configuration. There is no fleet-wide
rollout or automatic adaptive-learning restart.

## How it works

`archive_retention.py` runs on the hub with Python and Paramiko. It sends the
small `retention_agent.py` helper through SSH; the helper uses only Python's
standard library. The audio recorder is neither stopped nor reconfigured.
The helper confirms station identity and the exact recordings directory, hashes
completed WAVs and sidecars at low CPU priority, and caches hashes by inode,
size, modification time and change time. Unchanged files need no repeated Pi
read. New/missing hub copies are streamed to temporary files, flushed, hashed,
and atomically created without overwriting an existing file. NTFS or another
filesystem supporting hard links is required for the hub archive.

Before cleanup the hub writes a reviewable plan, freshly re-hashes each selected
archive, and sends batches of at most 500 verified receipts to the Pi. The Pi
rechecks file identity, cached digest, sidecar content and minimum age before
unlinking that exact WAV and matching sidecar. A changed or conflicting file is
retained for review. Per-file deletion records and a final status remain on the
hub. Hash equality confirms the saved bytes, not microphone quality or the
correctness of acoustic labels.

The Pi also keeps a small archive catalog so older clips stay visible through
the station dashboard and the hub's proxied event list. Playback uses the hub
copy. Heartbeat counts include that catalog, so local pruning does not reduce the
station's reported clip total. Archived-only rows are labelled and their local edit/delete controls are
hidden. The catalog holds the latest 20,000 archived names; all audio remains
on the hub even when a listing eventually exceeds that bound. Deploy the updated
`dashboard.py` and `noise_detector.py` before the first cleanup to preserve browsing and counts. The dashboard's
other existing module dependencies remain required.

## Configuration and operation

Keep `retention.private.json` outside Git and restrict it to the hub service
account. Install the hub dependency using `requirements-retention.txt`. Set `hub_recordings` to the real hub recordings directory and
`state_dir` to a separate private writable state directory. `stations` is an
array of objects containing `enabled`, `station_id`, `host`, `username`,
`known_hosts`, `root`, and either `key_filename` or `password`. The optional
`host_key_alias` reuses an already pinned identity when the station's Tailscale
address differs from its earlier LAN address. Unknown SSH keys fail closed.
Optional policy keys are `keep_days`, `max_local_bytes`, `min_free_bytes`, and
`minimum_age_seconds`. No credentials are printed or passed in command arguments.

```sh
# Audit only; writes hash caches and the plan, does not delete or copy audio.
python archive_retention.py --config /private/retention.private.json
# Fill missing archive copies and prepare the deletion plan, without deleting.
python archive_retention.py --config /private/retention.private.json --archive
# Verify, archive missing copies, then prune eligible local copies.
python archive_retention.py --config /private/retention.private.json --apply
```

On Windows, run `install_retention_task.ps1 -ConfigPath <private-config-path>
-PythonPath <python-exe-path>` from the installed script directory. It registers
an hourly task and a logon trigger under the current user, without storing a
Windows password. It runs while that account is signed in, including when the
screen is locked. It uses `pythonw.exe` when available so no terminal appears.
The hub computer must be on, signed in, and connected to Tailscale. Overlapping
runs are excluded by the scheduler and a process lock. Linux self-hosters can
schedule the same `--apply` command with their existing service manager.

Review `<station-id>-status.json`, `<station-id>-plan.json`, the deletion audit,
and rotated `retention.log` under `state_dir`. An unsuccessful task returns a
nonzero exit code and preserves unverified Pi copies. Disable the Windows task
to pause cleanup; recording and uploading continue. Restart the two Pi services
once when deploying catalog/count support; routine cleanup restarts neither.
The hub web service needs no restart.
