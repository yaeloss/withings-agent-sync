# withings-agent-sync

Private, zero-runtime-dependency Python sync from Withings to local JSON. It
fetches body composition, activity, and sleep summaries for local pipelines
and agents, writing a current snapshot and append-only history. Retrieved health
data is stored locally; API requests and OAuth credentials go only to Withings.

Requires Python 3.10+ on Linux or macOS.

## Install from GitHub

After the repository is published, install with [pipx](https://pipx.pypa.io/):

```bash
pipx install 'git+https://github.com/yaeloss/withings-agent-sync.git'
```

Or clone and install locally:

```bash
git clone https://github.com/yaeloss/withings-agent-sync.git
cd withings-agent-sync
pipx install .
```

Without pipx, use a virtual environment after cloning:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/withings-agent-sync --help
```

This is a GitHub-first project, not the unrelated `withings-sync` package on
PyPI. The Python module is `withings_agent_sync`.

## Setup

1. Create a public API application in the [Withings Developer
   Dashboard](https://developer.withings.com/dashboard/).
2. Register this exact callback URL:
   `http://127.0.0.1:8383/callback`
3. Request these scopes:
   `user.info,user.metrics,user.activity`.
   The current `user.activity` scope covers activity and sleep summaries;
   `user.sleep` is not a current Public API scope.
4. Create the client credentials file with owner-only permissions:

   ```bash
   install -d -m 700 ~/.config/withings-agent-sync
   umask 077
   cat >~/.config/withings-agent-sync/client.json <<'JSON'
   {"client_id": "YOUR_CLIENT_ID", "client_secret": "YOUR_CLIENT_SECRET"}
   JSON
   chmod 600 ~/.config/withings-agent-sync/client.json
   ```

5. Replace the placeholders above with your application's values, then authorize:

   ```bash
    withings-agent-sync authorize
   withings-agent-sync sync
   ```

The authorization code is short-lived. Keep the browser on the same machine as
the command, or copy the printed URL into a browser that can reach that
machine's loopback address. If you use `--port`, register the corresponding
callback URL in the Withings application first.

The default token file is `~/.local/state/withings-agent-sync/tokens.json` and
is written with mode `0600`. Do not put the credentials, token, snapshot, or
history files in a Git repository or a shared directory.

### Headless or SSH authorization

The callback is bound to `127.0.0.1`, never a public interface. For a remote
machine, forward its callback port before authorizing:

```bash
ssh -L 8383:127.0.0.1:8383 user@server
# In that remote shell:
withings-agent-sync authorize --no-browser
```

Keep the SSH connection open and open the printed URL in your local browser.
Authorization waits up to five minutes, rejects mismatched state and malformed
callbacks, and saves tokens only after a successful code exchange. The browser
response is an acknowledgement; the terminal confirms whether tokens were saved.

## Important Withings details

> **Gotchas**
>
> - Both authorization-code and refresh-token requests are POSTed to
>   `https://wbsapi.withings.net/v2/oauth2`, not `account.withings.com`.
> - `visceral_fat_index` (measure type 170) is an index/rating, not kilograms
>   or a percentage.
> - Withings rotates the refresh token. Every successful refresh replaces the
>   saved token file atomically.
> - Activity and sleep can return a non-zero API status when scopes or devices
>   are unavailable. The raw lists contain an error entry, the sync continues,
>   and summary counts exclude that error entry.
> - Withings currently advertises that Withings+ becomes required for the
>   “Start for Free” API plan from October 12, 2026. Check the [API
>   plans](https://developer.withings.com/developer-guide/v3/withings-solutions/withings-api-plans/)
>   page if authorization is unavailable.

## Output

By default, `withings-agent-sync sync` writes these private files in
`~/.local/state/withings-agent-sync/`:

- `withings_latest.json` — the complete current snapshot:

  ```json
  {
    "schema_version": 1,
    "status": "ok",
    "synced_at": "...",
    "days": 30,
    "userid": 123,
    "measures": [],
    "activity": [],
    "sleep": [],
    "summary": {
      "latest_measures": {},
      "latest_activity": null,
      "latest_sleep": null,
      "measure_count": 0,
      "activity_count": 0,
      "sleep_count": 0
    }
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
  | 91 | `pulse_wave_velocity_ms` | Pulse wave velocity in m/s, not milliseconds |
  | 123 | `vo2_max_ml_min_kg` | VO2 max |
  | 170 | `visceral_fat_index` | Visceral fat index/rating |

  `activity` contains daily Withings activity records. `sleep` contains sleep
  summary records, including `total_sleep_time` in seconds and `sleep_score`
  when the API/device provides them. `summary` contains the latest value and
  date for each measure field, latest valid activity and sleep records, and
  counts that exclude endpoint error markers.

  `status` is `partial` if activity or sleep returned an API error. Those
  failures also print a warning to stderr, even with `--quiet`; no provider
  error body is printed.

- `withings_sync_history.jsonl` — one private summary record per completed sync:

  ```json
  {"schema_version": 1, "status": "ok", "synced_at": "...", "summary": {"latest_measures": {}, "latest_activity": null, "latest_sleep": null, "measure_count": 0, "activity_count": 0, "sleep_count": 0}}
  ```

The fetchers follow Withings pagination, so a multi-page API response is
combined before sorting and summarizing.

Records are newest-first. Measurement timestamps are UTC; daily activity and
sleep dates are preserved as returned by Withings. Measurements request real
observations (`category=1`), not weight goals. `measure_count` counts measurement
groups, not individual fields. History stores summaries, not every raw record,
and repeated lookback windows can produce repeated summary values.

Partial sync exits `0`; automation that requires all endpoints should check
`status` in the JSON rather than rely only on the exit code.

Use `--out DIR` to select a different snapshot directory and `--history FILE`
to select a different history file. Token, credential, snapshot, history, and
lock paths must not alias one another. The data filenames are retained for
compatibility; the application and default directories use the new name.

## Privacy and reliability

- Credential and token inputs must be owner-owned regular files without
  group/other permissions (normally `0600`). Symlinked files are rejected.
- New output files and temporary files are private from creation. JSON snapshots
  and tokens are replaced atomically; existing directories are not silently
  chmodded. New leaf directories use mode `0700`.
- Authorization and sync share a token lock. Sync also locks its output files;
  overlapping processes fail safely rather than overwrite each other's state.
  Hidden `.lock` files remain after a run. Do not delete an active lock file.
- An unterminated history line stops the next sync before requests are made.
  Back up and repair that file manually; the program does not discard existing
  history. Readers should consume only complete, newline-terminated records.
- Snapshot replacement and history append are separate operations, not a
  multi-file transaction. A history write failure can leave a newer snapshot;
  the command exits nonzero. Compare `synced_at` when consistency matters.
- Requests have a 30-second timeout, reject redirects, and do not automatically
  retry one-time authorization codes or rotating refresh tokens. Error messages
  do not echo raw response bodies. Response size and pagination are bounded.

Use a local POSIX filesystem with reliable locking and rename semantics. File
permissions are not encryption and do not manage extended ACLs or backup access.
History grows until you manage its retention. This tool is not medical software
and is not affiliated with Withings. See [SECURITY.md](SECURITY.md).

Exit codes: `0` completed (including documented partial results), `2` invalid
configuration or usage, `1` API/network/storage errors or an overlapping run.

## Scheduling

Cron usually has a smaller `PATH` than an interactive shell. Use the absolute
path to the installed executable and make the environment explicit:

```cron
17 */5 * * * /home/you/.local/bin/withings-agent-sync sync --quiet
```

For a virtual environment, use `/home/you/venv/bin/withings-agent-sync`.

## Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `WITHINGS_TOKEN_FILE` | OAuth token JSON file | `~/.local/state/withings-agent-sync/tokens.json` |
| `WITHINGS_CREDS_FILE` | client credentials JSON file | `~/.config/withings-agent-sync/client.json` |
| `WITHINGS_OUT_DIR` | snapshot output directory | `~/.local/state/withings-agent-sync` |
| `WITHINGS_SYNC_DAYS` | number of days fetched by `sync` | `30` |
| `WITHINGS_CLIENT_ID` | client ID override | unset |
| `WITHINGS_CLIENT_SECRET` | client secret override | unset |

Set both credential environment variables together. Environment credentials
take precedence over the credentials file.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
python -m build
```

The tests never contact Withings. Live authorization and live health-data
smoke tests require the user's own credentials and are intentionally not part
of CI. Callback integration tests use ephemeral loopback ports and dummy data;
all configuration/state defaults are redirected into test directories. CI needs
no Withings credentials; do not add them as GitHub Actions secrets.

### Release checks

CI tests Python 3.10–3.14 on Linux, plus Python 3.10 and 3.14 on macOS. It
builds and validates both distribution formats, then installs each in a clean
environment outside the source checkout. The installed application has no
runtime dependencies.

Before publishing a release:

1. Run the tests and build commands above, then validate the packages:

   ```bash
   python -m pip install twine
   python -m twine check --strict dist/*
   ```

2. Review the files being committed and the contents of both packages for
   credentials or personal health data. Scan Git history as well as the
   working tree for secrets; `.gitignore` does not remove previously committed
   files.
3. Confirm GitHub CI is green, including the macOS jobs.
4. Privately smoke-test `authorize --no-browser` and `sync --quiet` with your
   own application credentials, including a token refresh. Keep all tokens
   and resulting health files outside the checkout, and never attach them to
   issues or release artifacts.

## License

MIT. See [LICENSE](LICENSE).
