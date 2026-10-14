---
name: withings-health
description: Read synced Withings weight, activity, and sleep data
version: 1.0.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [health, withings, quantified-self]
    category: health
---

# Withings health data

## When to Use
The user asks about their weight, body composition, blood pressure, heart rate,
activity, steps, or sleep recorded by Withings devices.

## Procedure
1. Read `~/.local/share/withings-agent-sync/withings_latest.json`.
2. Check `schema_version` is `1`. Note `synced_at`; if it is more than about
   12 hours old, say the data may be stale. If `status` is `partial`, activity
   or sleep is missing from this snapshot.
3. For current values, use `summary.latest_measures` (each entry has `value`
   and `date`), `summary.latest_activity`, and `summary.latest_sleep`.
4. For trends, use `measures` (newest first, UTC timestamps), `activity`, and
   `sleep` (local calendar dates).
5. Units are in the field names (`weight_kg`, `fat_ratio_pct`,
   `heart_rate_bpm`). `visceral_fat_index` has no unit. Sleep durations are in
   seconds.
6. A systemd timer refreshes the data. To refresh now, run
   `systemctl --user start withings-agent-sync.service`, then read the file
   again.

## Pitfalls
- Never read, print, copy, or summarize anything in
  `~/.local/state/withings-agent-sync/` or `~/.config/withings-agent-sync/`.
  They hold the Withings OAuth tokens and client secret.
- Never run `withings-agent-sync authorize`; it needs the user in a browser.
- Do not diagnose or give medical advice; this is personal tracking data.
- If the file is missing or old, check
  `systemctl --user status withings-agent-sync.timer` and
  `journalctl --user -u withings-agent-sync.service -n 20`. Exit status 3 means
  a partial sync; re-authorization is needed only if the log says so.

## Verification
Include the snapshot's `synced_at` time when reporting values.
