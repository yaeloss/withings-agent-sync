# withings-agent-sync

Private, zero-runtime-dependency Python sync from Withings to local JSON. It
fetches body composition, activity, and sleep summaries for local pipelines
and agents, writing a current snapshot and append-only history. Retrieved health
data is stored locally; API requests and OAuth credentials go only to Withings.

Requires Python 3.10+ on Linux or macOS.

> **Independent project:** withings-agent-sync is an independent, open-source
> tool. It is not affiliated with, endorsed by, or supported by Withings. It
> uses the official [Withings Public Health Data API](https://developer.withings.com/)
> through a developer application that you create and authorize yourself.
> Withings and related names are trademarks of their respective owner.

> **Withings plan requirement:** Withings says the free “Start for Free” API
> plan requires a Withings+ membership for Withings accounts created after
> October 12, 2026. Check the [Withings API plans](https://developer.withings.com/developer-guide/v3/withings-solutions/withings-api-plans/)
> page before troubleshooting an authorization failure.

## Install from GitHub

Install with [pipx](https://pipx.pypa.io/):

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
is written with mode `0600`. Health output is kept separately in
`~/.local/share/withings-agent-sync/`, so that directory can be shared with a
local agent without also exposing OAuth tokens. Do not put credentials, tokens,
or health files in a Git repository or an untrusted/shared directory.

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

### Hosted callback (paste mode)

If the callback registered in your Withings application is an HTTPS URL on
another server, use paste mode:

```bash
withings-agent-sync authorize \
  --redirect-uri https://callback.example/withings/callback \
  --no-browser
```

Open the printed URL, approve access, and Withings sends your browser to the
callback URL with `code` and `state` in its address. The callback page does not
need to work; only its address matters. Copy the full address from the browser
and paste it into the hidden prompt within about 30 seconds, before the code
expires. The command checks that the address and `state` match, then exchanges
the code itself. Credentials are never sent to the callback server.

The `--redirect-uri` value must exactly match the URL registered in the
Withings Developer Dashboard. The callback server receives the one-time code in
its access logs; the code expires quickly and is useless without your client
secret. Pages that rewrite or strip the address bar's query string break paste
mode.

## Run on a server (for example, a Hermes Agent host)

Install the CLI on the server and keep its credentials, tokens, and health data
there. These steps use a virtual environment so the server needs only Python
3.10+ with `venv`:

```bash
python3 -m venv ~/.local/venvs/withings-agent-sync
~/.local/venvs/withings-agent-sync/bin/python -m pip install \
  'git+https://github.com/yaeloss/withings-agent-sync.git'
```

Create `~/.config/withings-agent-sync/client.json` on the server as described
in [Setup](#setup), then authorize from an SSH session with paste mode (or an
SSH tunnel):

```bash
~/.local/venvs/withings-agent-sync/bin/withings-agent-sync authorize \
  --redirect-uri https://callback.example/withings/callback --no-browser
```

Withings rotates the refresh token whenever the access token is refreshed, so
tokens should live on one machine only. Do not put the client secret or tokens
in an agent's prompt, memory, skill files, or chat.

### Scheduled sync with systemd

Run syncs from the server's service manager, not from an agent conversation.
Templates are in [`deploy/systemd/`](deploy/systemd/); they assume the virtual
environment path above.

```bash
mkdir -p ~/.config/systemd/user
for unit in withings-agent-sync.service withings-agent-sync.timer; do
  curl -fsSLo ~/.config/systemd/user/$unit \
    https://raw.githubusercontent.com/yaeloss/withings-agent-sync/main/deploy/systemd/$unit
done
systemctl --user daemon-reload
systemctl --user enable --now withings-agent-sync.timer
sudo loginctl enable-linger "$USER"   # keep user timers running after logout
```

The timer runs every five hours and catches up after downtime. Check it with
`systemctl --user list-timers withings-agent-sync.timer` and
`journalctl --user -u withings-agent-sync.service`. A partial sync (exit code
`3`) marks the service failed so it is visible; the next run still happens.

## Add to Hermes Agent

These steps make your Withings data available to a
[Hermes Agent](https://hermes-agent.nousresearch.com/). Run them on the machine
where Hermes runs, as the same Linux user Hermes runs as. Hermes reads the
synced data; it never signs in to Withings or runs the sync itself.

1. **Install and authorize the CLI** on the Hermes host, as described in
   [Run on a server](#run-on-a-server-for-example-a-hermes-agent-host).
2. **Turn on scheduled syncs** with the
   [systemd timer](#scheduled-sync-with-systemd), then run one sync and confirm
   the snapshot exists:

   ```bash
   systemctl --user start withings-agent-sync.service
   ls -l ~/.local/share/withings-agent-sync/withings_latest.json
   ```

3. **Install the Hermes skill.**
   [`integrations/hermes/withings-health/SKILL.md`](integrations/hermes/withings-health/SKILL.md)
   tells Hermes where the snapshot is and how to read it. It also tells Hermes
   to report how fresh the data is and to stay out of the token and credential
   folders.

   ```bash
   hermes skills install \
     https://raw.githubusercontent.com/yaeloss/withings-agent-sync/main/integrations/hermes/withings-health/SKILL.md
   hermes skills list | grep withings-health
   ```

   Hermes shows the skill's security scan before installing. Open
   conversations pick up the new skill with your next message.

4. **Ask Hermes about your data**, either naturally or with the skill's slash
   command:

   ```text
   /withings-health How has my weight changed over the last two weeks?
   ```

   Hermes should answer from the snapshot and mention its `synced_at` time.

5. **Optional: get a daily summary** with a Hermes cron job. Cron jobs start a
   fresh session, so attach the skill and keep the prompt self-contained:

   ```bash
   hermes cron create "0 8 * * *" \
     "Summarize my latest Withings weight, activity, and sleep in three bullets. Mention when the data was synced and flag a partial or stale sync." \
     --skill withings-health
   ```

   Add `--deliver` to send the summary to a messaging platform; see the
   [Hermes cron docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron).

### Keep your Withings access private

- Never paste the client secret, tokens, or raw health files into a Hermes
  chat. Hermes' memory and session history may keep them.
- Do not ask Hermes to run `withings-agent-sync authorize`. Re-authorize
  yourself over SSH when the token expires or is revoked.
- The data files are owner-only (`0600`), so Hermes must run as the same Linux
  user as the timer to read them. That also means file permissions cannot stop
  Hermes from reading the tokens; the skill's instructions are guidance, not a
  security boundary. For real isolation, run Hermes' terminal backend in a
  container that mounts only `~/.local/share/withings-agent-sync/` read-only.

### Update

```bash
~/.local/venvs/withings-agent-sync/bin/python -m pip install --upgrade \
  'git+https://github.com/yaeloss/withings-agent-sync.git'
hermes skills install \
  https://raw.githubusercontent.com/yaeloss/withings-agent-sync/main/integrations/hermes/withings-health/SKILL.md
```

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
> - For the current Withings+ requirement affecting newer accounts, see the
>   plan notice near the top of this README.

## Output

By default, `withings-agent-sync sync` writes these private files in
`~/.local/share/withings-agent-sync/`:

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
  {"schema_version": 1, "status": "ok", "synced_at": "...", "summary": {"latest_measures": {}, "measure_count": 0, "activity_count": 0, "sleep_count": 0, "latest_activity_date": null, "latest_sleep_date": null}}
  ```

The fetchers follow Withings pagination, so a multi-page API response is
combined before sorting and summarizing.

Records are newest-first. Measurement timestamps are UTC; daily activity and
sleep dates are preserved as returned by Withings. Activity and sleep requests
end one day after the current UTC date so the current local day is included in
time zones ahead of UTC. Measurements request real observations (`category=1`),
not weight goals. `measure_count` counts measurement groups, not individual
fields. History stores counts, latest measurement values, and the dates of the
latest activity and sleep records—not the raw records. Repeated lookback
windows can produce repeated summary values.

A partial sync exits `3`; this means the snapshot was saved but activity or
sleep was unavailable. Automation can treat `0` as complete, `3` as usable but
incomplete, `2` as invalid configuration/usage, and `1` as an API, network,
storage, or concurrency failure.

Use `--out DIR` to select a different snapshot directory and `--history FILE`
to select a different history file. Token, credential, snapshot, history, and
lock paths must not alias one another, and `sync` refuses an output directory
that contains the token or credentials file. The data filenames are retained
for compatibility; the application and default directories use the new name.

## Using with AI agents

Schedule `withings-agent-sync sync --quiet` yourself (for example with cron),
then give the agent read access to the data directory only:

```text
~/.local/share/withings-agent-sync/withings_latest.json
```

- Do not give agents access to `~/.local/state/withings-agent-sync/` (OAuth
  tokens) or `~/.config/withings-agent-sync/` (client secret). An agent that can
  run shell commands as your user can read those files regardless of their
  permissions, so prefer scheduled syncs over letting the agent run `sync`.
- Check `schema_version` (currently `1`) and `status` before using the data.
  `partial` means activity or sleep is missing from this snapshot.
- Use `summary` for the latest values; `measures`, `activity`, and `sleep` hold
  the full lookback window.
- Treat the contents as personal health data, not medical advice.

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

Exit codes: `0` completed, `3` completed with unavailable activity or sleep,
`2` invalid configuration or usage, `1` API/network/storage errors or an
overlapping run.

## Disconnect and delete data

1. Remove the scheduled job, then delete local files:

   ```bash
   rm -rf ~/.local/share/withings-agent-sync ~/.local/state/withings-agent-sync ~/.config/withings-agent-sync
   pipx uninstall withings-agent-sync
   ```

   Adjust the paths if you set `WITHINGS_*` variables, `--out`, or `--history`.
2. Revoke the app's access to your Withings account, or delete the application
   in the [Withings Developer Dashboard](https://developer.withings.com/dashboard/).

## Scheduling

Cron usually has a smaller `PATH` than an interactive shell. Use the absolute
path to the installed executable and make the environment explicit:

```cron
17 */5 * * * /home/you/.local/bin/withings-agent-sync sync --quiet
```

For a virtual environment, use `/home/you/venv/bin/withings-agent-sync`.
Cron reports nonzero exits, including `3` for a partial sync.

## Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `WITHINGS_TOKEN_FILE` | OAuth token JSON file | `~/.local/state/withings-agent-sync/tokens.json` |
| `WITHINGS_CREDS_FILE` | client credentials JSON file | `~/.config/withings-agent-sync/client.json` |
| `WITHINGS_OUT_DIR` | snapshot output directory | `~/.local/share/withings-agent-sync` |
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
