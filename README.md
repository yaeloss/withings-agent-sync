# withings-sync

Zero-dependency Python sync from Withings to local JSON. It fetches body
composition, activity, and sleep data for use by pipelines and agents, writing
a current snapshot and an append-only history file.

## Setup

1. Create a Withings developer application at <https://oauth.withings.com>.
   Register this callback URL:
   `http://127.0.0.1:8383/callback`
2. Enable these scopes: `user.info,user.metrics,user.activity,user.sleep`.
3. Create the client credentials file (by default
   `~/.config/withings-sync/client.json`):

   ```json
   {"client_id": "YOUR_CLIENT_ID", "client_secret": "YOUR_CLIENT_SECRET"}
   ```

4. Install the package, or run it from a clone:

   ```bash
   pipx install .
   # alternatively: python3 -m pip install .
   ```

5. Authorize the Withings account once, then sync:

   ```bash
   withings-sync authorize
   withings-sync sync
   ```

   The authorization helper opens a browser and saves tokens with file mode
   `0600`. If you use `--port`, register the corresponding callback URL in the
   Withings application first.

## Important Withings details

> **Gotchas**
>
> - Both authorization-code and refresh-token requests are POSTed to
>   `https://wbsapi.withings.net/v2/oauth2`, not `account.withings.com`.
> - `visceral_fat_index` (measure type 170) is an index/rating, not kilograms
>   or a percentage.
> - Activity and sleep can return a non-zero API status when scopes or devices
>   are unavailable. The sync records an error entry and continues instead of
>   crashing.

## Output

By default, `withings-sync sync` writes these files in
`~/.local/state/withings-sync/`:

- `withings_latest.json` — the complete current snapshot:

  ```json
  {
    "synced_at": "...",
    "days": 30,
    "userid": 123,
    "measures": [],
    "activity": [],
    "sleep": [],
    "summary": {}
  }
  ```

  Each item in `measures` includes `date`, `timestamp`, `grpid`, and
  `category`, followed by whichever normalized fields the device returned:

  | Withings type | Output field | Meaning |
  | ---: | --- | --- |
  | 1 | `weight_kg` | Weight |
  | 4 | `height_m` | Height |
  | 5 | `fat_free_mass_kg` | Fat-free mass |
  | 6 | `fat_ratio_pct` | Fat ratio |
  | 8 | `fat_mass_kg` | Fat mass |
  | 9 | `diastolic_bp_mmhg` | Diastolic blood pressure |
  | 10 | `systolic_bp_mmhg` | Systolic blood pressure |
  | 11 | `heart_rate_bpm` | Heart rate |
  | 12 | `temperature_c` | Temperature |
  | 54 | `spo2_pct` | Blood oxygen saturation |
  | 71 | `body_temperature_c` | Body temperature |
  | 73 | `skin_temperature_c` | Skin temperature |
  | 76 | `muscle_mass_kg` | Muscle mass |
  | 77 | `hydration_kg` | Hydration |
  | 88 | `bone_mass_kg` | Bone mass |
  | 91 | `pulse_wave_velocity_ms` | Pulse wave velocity |
  | 123 | `vo2_max_ml_min_kg` | VO2 max |
  | 170 | `visceral_fat_index` | Visceral fat index/rating |

  `activity` contains Withings activity records. `sleep` contains sleep
  summary records, including `total_sleep_time` in seconds and `sleep_score`
  when the API/device provides them. `summary` contains the latest value and
  date for each measure field, latest activity and sleep records, and counts.

- `withings_sync_history.jsonl` — one JSON object per successful sync, with
  this shape:

  ```json
  {"synced_at": "...", "summary": {"measure_count": 0, "activity_count": 0, "sleep_count": 0}}
  ```

Use `--out DIR` to select a different snapshot directory and `--history FILE`
to select a different history file.

## Scheduling

For example, run every five hours at 17 minutes past the hour:

```cron
17 */5 * * * withings-sync sync --quiet
```

## Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `WITHINGS_TOKEN_FILE` | OAuth token JSON file | `~/.local/state/withings-sync/tokens.json` |
| `WITHINGS_CREDS_FILE` | client credentials JSON file | `~/.config/withings-sync/client.json` |
| `WITHINGS_OUT_DIR` | snapshot output directory | `~/.local/state/withings-sync` |
| `WITHINGS_SYNC_DAYS` | number of days fetched by `sync` | `30` |
| `WITHINGS_CLIENT_ID` | client ID override | unset |
| `WITHINGS_CLIENT_SECRET` | client secret override | unset |

Environment values override the corresponding file/default where applicable.
Keep credential and token files outside version control.

## License

MIT. See [LICENSE](LICENSE).
