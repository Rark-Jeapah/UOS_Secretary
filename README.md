# Sidae Secretary

Local-first macOS sync agent for the current University of Seoul Telegram beta.

## Current Beta-Critical Path

- UOS official API / UOS portal fallback for timetable and course metadata
- UClass/Moodle for materials and assignments
- Telegram commands and morning/evening briefings
- per-user weather
- UOS general/academic notices

## Optional / Non-Critical For This Milestone

- iCloud dashboard and material archive
- macOS GUI launcher
- relay-based external briefing delivery

Runtime model:

- Local SQLite DB is the source of truth for Telegram replies, reminders, briefings, and cached UOS/UClass data.
- The beta-critical runtime centers on UOS portal/UClass/weather/Telegram. iCloud publish remains an optional output.
- UClass materials and dashboard artifacts can still be written to iCloud Drive for offline viewing/archive when that optional surface is enabled.
- The recommended Telegram path is a long-running `telegram-listener` process, not a minute-based poller.

Refactored module boundaries:

- Telegram reply/view state:
  - `src/sidae_secretary/telegram_setup_state.py`
  - `src/sidae_secretary/day_agenda_state.py`
  - `src/sidae_secretary/ops_health_state.py`
- Onboarding/application services:
  - `src/sidae_secretary/onboarding_service.py`
  - `src/sidae_secretary/onboarding_school_connect.py`
  - `src/sidae_secretary/portal_sync_service.py`
- Ops dashboard:
  - `src/sidae_secretary/ops_snapshot_service.py`
  - `src/sidae_secretary/ops_action_service.py`
  - `src/sidae_secretary/ops_dashboard.py`
  - `src/sidae_secretary/ops_dashboard_assets/dashboard.html`
- DB read/write facades under `src/sidae_secretary/db.py` now delegate to:
  - `src/sidae_secretary/db_auth_attempts.py`
  - `src/sidae_secretary/db_connections.py`
  - `src/sidae_secretary/db_sync.py`
  - `src/sidae_secretary/db_dashboard_queries.py`
- CLI command registration:
  - `src/sidae_secretary/cli.py` keeps root wiring/shared helpers
  - `src/sidae_secretary/cli_ops.py`
  - `src/sidae_secretary/cli_onboarding.py`
  - `src/sidae_secretary/cli_admin.py`
  - `src/sidae_secretary/cli_launchd.py`

## Quick Start

1. Create and activate a Python 3.11+ virtual environment.
   - Use an OpenSSL-backed Python build (Homebrew/python.org), not macOS system Python.
   - Example: `python3.11 -m venv .venv && source .venv/bin/activate`
   - Verify SSL backend: `python -c "import ssl; print(ssl.OPENSSL_VERSION)"`
2. Install dependencies:
   - `pip install -e .`
3. Copy `.env.example` to `.env` and fill required values (or use `config.example.toml`).
   - UClass auth: either `UCLASS_WSTOKEN`, or `UCLASS_USERNAME` + `UCLASS_PASSWORD`.
4. Run the beta-critical path:
   - `sidae doctor`
   - Set `ONBOARDING_PUBLIC_BASE_URL` to the public HTTPS origin that will terminate `/moodle-connect` and forward it to `sidae onboarding serve`.
   - For a closed UOS beta, set `ONBOARDING_ALLOWED_SCHOOL_SLUGS=["uos_online_class","uos_portal"]`.
   - Do not point users at a raw `http://host:port/...` onboarding URL; that exposes the login flow without TLS.
   - `sudo .venv/bin/sidae launchd install-uclass-poller --interval-minutes 60 --connectivity-check-seconds 30 --sync-timeout-seconds 600 --scope daemon --run-as-user <mac_user>`
   - `sudo .venv/bin/sidae launchd install-weather-sync --minute-offset 20 --sync-timeout-seconds 600 --scope daemon --run-as-user <mac_user>`
   - `sudo .venv/bin/sidae launchd install-telegram-listener --poll-timeout-seconds 10 --error-backoff-seconds 2 --max-consecutive-errors 6 --scope daemon --run-as-user <mac_user>`
   - `sudo .venv/bin/sidae launchd install-onboarding --host 127.0.0.1 --port 8791 --scope daemon --run-as-user <mac_user>`
   - `sidae status`
   - Optional add-ons:
     - `sudo .venv/bin/sidae launchd install-ops-dashboard --host 127.0.0.1 --port 8793 --scope daemon --run-as-user <mac_user>`
     - `sidae launchd install-publish --interval-minutes 60 --scope agent`

Recommended topology:

- Development machine: editor, browser, and local tests.
- Runtime machine: always-on host for UOS/UClass sync, Telegram, and onboarding.
- Remote management: Tailscale or LAN SSH for shell access. Screen Sharing is only needed for optional GUI or browser-driven automation.
- Beta test deployment: commit locally, then push the tested branch to the beta deployment ref.
- Production promotion: after beta validation, push the same tested commit to the prod deployment ref.

Recommended always-on Mac setup:

- Keep the Mac logged in, connected to power, and out of system sleep.
- Let the UOS portal onboarding/session path handle timetable and course metadata for the current beta.
- Let `uclass-poller` handle UClass ingestion.
- Let `weather-sync` handle KMA weather and Seoul district air-quality refresh on a fixed hourly cadence.
- Let `telegram-listener` handle Telegram commands, `/plan` reminders, and 09:00 / 21:00 briefings.
- Keep `uclass-poller`, `weather-sync`, `telegram-listener`, and `onboarding` in `daemon` scope if the Mac is intended to behave like a headless server.
- Set `BRIEFING_DELIVERY_MODE=direct` if this Mac itself should send the 09:00 / 21:00 Telegram briefings.
- Optional add-ons:
  - Use `sidae publish` when you want refreshed iCloud dashboard artifacts or precomputed external-briefing files.
  - Keep browser-driven helpers in `agent` scope only when you explicitly need those optional surfaces.

Delivery modes:

- `BRIEFING_DELIVERY_MODE=direct`
  - Recommended for the current beta-critical path on an always-on Mac.
  - `telegram-listener` sends 09:00 / 21:00 briefings directly from the local SQLite DB.
- `BRIEFING_DELIVERY_MODE=precompute_only`
  - Optional/non-critical path only. Use it when another scheduled sender (for example iPhone Shortcut or relay client) will deliver the messages.
  - In this mode use `sidae publish` artifacts or the signed relay payloads instead of direct Mac-side Telegram sends.

## CLI

- `sidae doctor`  
  Validate config and dependencies, initialize SQLite DB, and print the resolved feature-flag state.
- `sidae doctor --fix`  
  Create missing local folders and print setup next steps.
- `sidae init`  
  Interactive wizard that writes `config.toml` and `.env`.
- `sidae sync --all [--wait --timeout-seconds N]`  
  Run one full pipeline pass: UClass -> UOS portal timetable -> Weather -> Telegram -> scheduled briefings -> local summaries -> daily digest -> optional storage publish.
- `sidae sync-uclass [--wait --timeout-seconds N]`  
  Run only the UClass ingestion path plus local review-event regeneration.
- `sidae sync-weather [--wait --timeout-seconds N]`  
  Run the local environment snapshot sync: KMA weather for the configured location plus Seoul district air quality for the configured district codes.
- `sidae sync-telegram [--wait --timeout-seconds N]`  
  Run one Telegram polling batch plus reminder dispatch.
- `sidae telegram-listener [--poll-timeout-seconds 10 --error-backoff-seconds 2 --max-consecutive-errors 6]`  
  Keep a long-running Telegram long-poll listener alive for near-immediate command replies, `/plan` reminder delivery, and direct 09:00 / 21:00 briefing sends.
- `sidae send-briefings [--wait --timeout-seconds N]`  
  Run only the scheduled Telegram morning/evening briefing sender once. Mainly useful for manual checks or legacy split scheduling.
- `sidae uclass-poller [--interval-minutes 60 --connectivity-check-seconds 30 --sync-timeout-seconds 600]`  
  Keep a UClass-first collector alive. It triggers once when the UClass host becomes reachable and then once per interval while connectivity stays up.
- `sidae launchd install-weather-sync [--minute-offset 20 --sync-timeout-seconds 600] [--scope agent|daemon] [--run-as-user USER]` / `sidae launchd uninstall-weather-sync [--scope agent|daemon]`  
  Install or remove a fixed hourly weather + Seoul air-quality sync job. The default `:20` offset avoids the `:00-:15` window where the Seoul cleanair site may still show the previous hour's data.
- `sidae onboarding serve [--host 127.0.0.1 --port 8791]`  
  Run the Telegram-driven onboarding web server behind a public HTTPS URL set by `ONBOARDING_PUBLIC_BASE_URL`.
  Handles Moodle credential onboarding plus the UOS portal/browser follow-up paths used by `/connect`.
- `sidae onboarding browser-login --school <name> --chat-id <id>`  
  Open a persistent browser profile for schools that require browser-session onboarding.
- `sidae publish`  
  Optional: render dashboard snapshot to the configured storage root.
  Also exports precomputed Telegram briefing files for today/tomorrow date-slots so another sender
  (for example, an iPhone Shortcut or cloud worker) can deliver the last-sync snapshot even while the Mac is offline.
- `sidae ops serve [--host 127.0.0.1 --port 8793]`  
  Run the live local-only operations dashboard on the runtime Mac.
- `sidae ops open-remote [--ssh-host <runtime-host> --open-browser]`
  Open the runtime Mac's local-only ops dashboard on the operator Mac through an SSH local tunnel.
- `sidae gui`  
  Optional: open a small macOS control panel that lets you choose full sync, Telegram-only polling, or status.
  The selected job runs in Terminal, then a Korean summary popup is shown when it finishes.
  A Finder-friendly launcher is also available at `deploy/macos/Sidae Secretary GUI.command`.
  For the live ops dashboard, a Finder-friendly launcher is also available at `deploy/macos/Sidae Ops Dashboard.command`.
- `sidae status`  
  Show last run state and DB item counts.
  Includes `feature_flags` with stable Telegram rollout keys:
  `TELEGRAM_COMMANDS_ENABLED`, `TELEGRAM_SMART_COMMANDS_ENABLED`,
  `TELEGRAM_ASSISTANT_ENABLED`, `TELEGRAM_ASSISTANT_WRITE_ENABLED`.
  Includes dependency readiness in `deps` with stable keys:
  `telegram_import_ok`, `llm_import_ok`,
  `telegram_requests_import_ok`, `telegram_dateutil_import_ok`,
  `llm_provider_supported`, `llm_provider_import_ok`,
  `icalendar_import_ok`.
- `sidae verify mobile-offline [--materials-check-limit N|--materials-check-all]`  
  Optional: validate iPhone/iCloud offline readiness and choose sampled vs exhaustive material checks.
- `sidae verify auth-attempts`  
  Show recent onboarding auth attempts and suspicious remote sources.
- `sidae verify closed-loop [--timeout-seconds N]`  
  Run doctor readiness -> `sync --all --wait --timeout N` -> `verify mobile-offline` and emit one JSON report.
- `sidae docs-artifacts [--check] [--require-clean-git]`  
  Sync/validate `docs/snapshot.json`, `docs/audit.json`, `docs/SNAPSHOT.md`, `docs/AUDIT.md` metadata consistency.
  Supports explicit mode: `sidae docs-artifacts sync` or `sidae docs-artifacts check`.
- `sidae ack identity [--token <token>] [--expires-hours N]`  
  Record an explicit human ACK required by the privacy gate when `INCLUDE_IDENTITY=true`.
- `sidae tasks list --open`  
  List open tasks.
- `sidae tasks done --id <row_id|external_id>` / `sidae tasks ignore --id <row_id|external_id>`  
  Mark tasks complete or ignored.
- `sidae admin refresh-user`  
  Refresh beta-critical surfaces for one user without running the full sync pipeline.
- `sidae admin last-failed-stage`  
  Inspect the most recent failed or degraded beta-critical stage.
- `sidae buildings set --number <n> --name <name>` / `sidae buildings import --csv <file>` / `sidae buildings list`  
  Manage building number mappings used for class location expansion.
- `sidae buildings seed-uos [--overwrite]`  
  Seed the built-in University of Seoul building map into DB.
- `sidae courses list [--aliases]` / `sidae courses resolve --alias <text>` / `sidae courses alias-add --course <selector> --alias <text>` / `sidae courses alias-remove --course <selector> --alias <text>`  
  Inspect canonical course entities and manage manual alias bindings used by `/today` and briefing matching.
- `sidae reminders list [--status pending|sent|failed|cancelled]`  
  Inspect scheduled Telegram reminders created by `/plan`.
- `sidae uclass probe [--write-json|--json-out data/uclass_probe.json]`  
  Probe configured Moodle wsfunctions and print an OK/FAIL/SKIP matrix.
- `sidae inbox list`  
  Show unprocessed inbox drafts.
- `sidae inbox apply --id <id> | --all`  
  Convert drafts into actionable DB records.
- `sidae inbox ignore --id <id>`  
  Mark inbox item as processed/ignored.
- `sidae portal import --ics-url ... | --ics-file ... | --csv ...`  
  Import academic dates and upsert to DB.
- `sidae launchd install --time HH:MM[,HH:MM...] [--sync-timeout-seconds N] [--scope agent|daemon] [--run-as-user USER]` / `sidae launchd uninstall [--scope agent|daemon]`  
  Install or remove one or more daily launchd schedules.
- `sidae launchd install-uclass-poller [--interval-minutes 60 --connectivity-check-seconds 30 --sync-timeout-seconds 600] [--scope agent|daemon] [--run-as-user USER]` / `sidae launchd uninstall-uclass-poller [--scope agent|daemon]`  
  Install or remove the long-running UClass-only collector.
- `sidae launchd install-weather-sync [--minute-offset 20 --sync-timeout-seconds 600] [--scope agent|daemon] [--run-as-user USER]` / `sidae launchd uninstall-weather-sync [--scope agent|daemon]`  
  Install or remove a fixed hourly weather + Seoul air-quality sync job.
- `sidae launchd install-onboarding [--host 127.0.0.1 --port 8791] [--scope agent|daemon] [--run-as-user USER]` / `sidae launchd uninstall-onboarding [--scope agent|daemon]`  
  Install or remove the long-running Moodle onboarding web server used by `/connect`.
- `sidae launchd install-ops-dashboard [--host 127.0.0.1 --port 8793] [--scope agent|daemon] [--run-as-user USER]` / `sidae launchd uninstall-ops-dashboard [--scope agent|daemon]`  
  Install or remove the live local-only ops dashboard service for the runtime Mac.
- `sidae launchd install-publish [--interval-minutes 60] [--scope agent|daemon] [--run-as-user USER]` / `sidae launchd uninstall-publish [--scope agent|daemon]`  
  Optional: install or remove an iCloud dashboard publish job for the current local DB snapshot.
- `sidae launchd install-telegram-poller [--interval-minutes N] [--sync-timeout-seconds N] [--scope agent|daemon] [--run-as-user USER]` / `sidae launchd uninstall-telegram-poller [--scope agent|daemon]`  
  Install or remove a lightweight Telegram-only poller. This is the legacy batch mode; prefer `telegram-listener`.
- `sidae launchd install-telegram-listener [--poll-timeout-seconds 10 --error-backoff-seconds 2 --max-consecutive-errors 6] [--scope agent|daemon] [--run-as-user USER]` / `sidae launchd uninstall-telegram-listener [--scope agent|daemon]`  
  Install or remove the long-running Telegram listener. This is the recommended Telegram runtime.
- `sidae launchd install-briefings [--time 09:00,21:00 --sync-timeout-seconds 120] [--scope agent|daemon] [--run-as-user USER]` / `sidae launchd uninstall-briefings [--scope agent|daemon]`  
  Install or remove the fixed-time Telegram briefing sender. Useful only when briefings are split away from `telegram-listener`.
- `sidae launchd install-relay [--host 0.0.0.0 --port 8787] [--scope agent|daemon] [--run-as-user USER]` / `sidae launchd uninstall-relay [--scope agent|daemon]`  
  Optional: install or remove a KeepAlive launchd job for the signed briefing relay.
- `sidae relay serve [--host 127.0.0.1 --port 8787 --state-file ...]`  
  Optional: run a signed relay that accepts precomputed briefing payloads and delivers them to Telegram without exposing the bot token to iPhone Shortcuts.
- `sidae export --json-out data/export.json` / `sidae import --json <file>`  
  Export/import local DB state as JSON.
- `sidae backup --to-icloud`  
  Create a timestamped iCloud backup zip with DB + JSON snapshot.

## Telegram Commands

- `/start`
  - Welcome message and first-use guidance in Korean section format.
- `/help`
  - User-facing command summary grouped into basic and management commands.
- `/connect [school name]`
  - Issues a one-time onboarding link for supported school-account flows.
  - For the current UOS product path, the built-in directory can bundle online-classroom connect with portal/timetable bootstrap behind the same onboarding flow.
  - If the school name matches the built-in directory, the web form preselects the official LMS URL and may route through direct credential or browser-session follow-up depending on the school's auth mode.
- `/setup`
  - Current connection checklist for Telegram, UClass, UOS portal timetable access, and optional integrations, plus recommended next commands.
- `/status`
  - Stored counts, last successful sync, tracked source statuses, Telegram activity, and recent UClass result.
- `/today`
  - Today's meetings, classes, due tasks, and shortcut to detailed material summaries.
- `/tomorrow`
  - Tomorrow's meetings, classes, due tasks, and shortcut to detailed material summaries.
- `/todaysummary`
  - Brief summaries for today's class materials.
- `/tomorrowsummary`
  - Brief summaries for tomorrow's class materials.
- `/weather`
  - Current weather plus today's morning/afternoon forecast, tomorrow overview, and the latest Seoul district fine-dust readings from the last local sync.
  - `/todayweather` remains available as a backward-compatible alias.
- `/region <location>`
  - Update the chat's default weather region/district used by `/weather`, briefings, and assistant weather reads.
- `/notice_uclass`
  - The 10 most recent online-classroom notifications stored by the latest UClass sync, rendered with source and last-sync context.
- `/notice_general`
  - The 10 most recent school general notices from the portal.
- `/notice_academic`
  - The 10 most recent school academic notices from the portal.
- `/inbox`
  - Inspect unprocessed draft items created from free-form Telegram messages, grouped with typed labels such as 일정/과제/메모.
- `/apply <id|all>`
  - Convert inbox draft(s) into real tasks/events.
- `/done task <id|external_id>`
  - Mark a task done.
- `/plan <natural language instruction>`
  - Available only when `TELEGRAM_SMART_COMMANDS_ENABLED=true`.
  - Example: `/plan tomorrow 8am remind me to submit HW`
  - Parsed result is stored in `telegram_reminders`, then dispatched automatically by `telegram-listener` or `sync-telegram`.
- `/bot <natural language request>`
  - Available only when `TELEGRAM_ASSISTANT_ENABLED=true`.
  - Read-only requests work by default; state-changing assistant actions additionally require `TELEGRAM_ASSISTANT_WRITE_ENABLED=true`.
  - Typical current actions are schedule/weather reads, one-time reminder creation, notification policy updates, and weather region changes.
- If a chat is not in `TELEGRAM_ALLOWED_CHAT_IDS`, command replies stay blocked until `/setup` is used and the chat is explicitly allowed.

## launchd

You can still use the template:

- `deploy/launchd/com.sidae.secretary.plist.template`

Or install directly from CLI:

Beta-critical path:
- `sidae launchd install-uclass-poller --interval-minutes 60 --connectivity-check-seconds 30 --sync-timeout-seconds 600 --scope daemon --run-as-user <mac_user>`
- `sidae launchd install-weather-sync --minute-offset 20 --sync-timeout-seconds 600 --scope daemon --run-as-user <mac_user>`
- `sidae launchd install-onboarding --host 127.0.0.1 --port 8791 --scope daemon --run-as-user <mac_user>`
- `sidae launchd install-telegram-listener --poll-timeout-seconds 10 --error-backoff-seconds 2 --max-consecutive-errors 6 --scope daemon --run-as-user <mac_user>`

Optional/non-critical add-ons:
- `sudo .venv/bin/sidae launchd install-ops-dashboard --host 127.0.0.1 --port 8793 --scope daemon --run-as-user <mac_user>`
- `sidae launchd install-publish --interval-minutes 60 --scope agent`
- `sidae launchd install-relay --host 0.0.0.0 --port 8787 --scope agent`

Optional legacy batch jobs:
- `sidae launchd install --time 08:50,20:50 --sync-timeout-seconds 600 --scope daemon --run-as-user <mac_user>`
- `sidae launchd install-telegram-poller --interval-minutes 5 --sync-timeout-seconds 120 --scope daemon --run-as-user <mac_user>`
- `sidae launchd install-briefings --time 09:00,21:00 --sync-timeout-seconds 120 --scope agent`

Scope guidance:
- `--scope agent` writes to `~/Library/LaunchAgents` and loads into `gui/<uid>`.
- `--scope daemon` writes to `/Library/LaunchDaemons`, loads into `system`, and requires `--run-as-user`.
- Use `daemon` for headless collectors and listeners that should survive logout.
- Keep optional browser-driven jobs in `agent` scope only when you explicitly need them.
- All launchd install/uninstall commands support `--instance-name <name>` for parallel runtimes.
  Leave it empty for the default production instance, or set `INSTANCE_NAME = "beta"` in the beta config so labels and log files stay separate automatically.

Generated launchd jobs now pin the exact Python executable:
- `<sys.executable> -m sidae_secretary.cli sync --all --wait ...`
- `--config-file` is always written as an absolute path.
- `WorkingDirectory` is set to the resolved config directory.

If you change virtualenv or Python binary, reinstall the job:
- `sudo .venv/bin/sidae launchd uninstall-uclass-poller --scope daemon`
- `sudo .venv/bin/sidae launchd install-uclass-poller --interval-minutes 60 --connectivity-check-seconds 30 --sync-timeout-seconds 600 --scope daemon --run-as-user <mac_user>`
- `sudo .venv/bin/sidae launchd uninstall-weather-sync --scope daemon`
- `sudo .venv/bin/sidae launchd install-weather-sync --minute-offset 20 --sync-timeout-seconds 600 --scope daemon --run-as-user <mac_user>`
- `sudo .venv/bin/sidae launchd uninstall-telegram-listener --scope daemon`
- `sudo .venv/bin/sidae launchd install-telegram-listener --poll-timeout-seconds 10 --error-backoff-seconds 2 --max-consecutive-errors 6 --scope daemon --run-as-user <mac_user>`

Optional add-ons:
- `sudo .venv/bin/sidae launchd uninstall-ops-dashboard --scope daemon`
- `sudo .venv/bin/sidae launchd install-ops-dashboard --host 127.0.0.1 --port 8793 --scope daemon --run-as-user <mac_user>`
- `sidae launchd uninstall-publish --scope agent`
- `sidae launchd install-publish --interval-minutes 60 --scope agent`

Optional legacy batch jobs:
- `sudo .venv/bin/sidae launchd uninstall --scope daemon`
- `sudo .venv/bin/sidae launchd install --time 08:50,20:50 --sync-timeout-seconds 600 --scope daemon --run-as-user <mac_user>`
- `sudo .venv/bin/sidae launchd uninstall-telegram-poller --scope daemon`
- `sudo .venv/bin/sidae launchd install-telegram-poller --interval-minutes 5 --sync-timeout-seconds 120 --scope daemon --run-as-user <mac_user>`
- `sidae launchd uninstall-briefings --scope agent`
- `sidae launchd install-briefings --time 09:00,21:00 --sync-timeout-seconds 120 --scope agent`

Runtime note:
- Optional interval-based jobs such as `publish` often show `state = not running` between scheduled executions. That is normal unless the last run recorded an error.

## Git Deployment

- Keep one always-on runtime machine and push code from a separate development machine.
- Leave `.env`, `data/`, and any local credential folders on the runtime Mac; they are deployment state, not git content.
- Keep local development separate from production runtime state.
- Recommended paths:
  - Bare repo: `/path/to/git/UOS_secretary.git`
  - Prod app tree: `/path/to/apps/UOS_secretary`
  - Beta app tree: `/path/to/apps/UOS_secretary_beta`
- Recommended beta config:
  - `INSTANCE_NAME = "beta"`
  - `SECRET_STORE_BACKEND = "file"` so `onboarding` and `telegram-listener` do not depend on the same login keychain session
  - `ONBOARDING_ALLOWED_SCHOOL_SLUGS = ["uos_online_class", "uos_portal"]`
  - `TELEGRAM_SMART_COMMANDS_ENABLED = true`
  - `TELEGRAM_ASSISTANT_ENABLED = true`
  - `TELEGRAM_ASSISTANT_WRITE_ENABLED = true`
  - `LLM_ENABLED = true` so beta is the real `/bot` validation surface before prod promotion
  - separate `DATABASE_PATH`, `STORAGE_ROOT_DIR`, Telegram bot token, and secret-store files from prod
  - start from `config.beta.example.toml` instead of reusing prod config
- Install the example bare-repo hook from `deploy/git/post-receive.bare.example` and adjust the placeholder paths.
- The hook can map multiple deploy branches, for example `deploy` -> prod app tree and `beta` -> beta app tree, then runs `deploy/git/redeploy.sh` for the matching instance.
- Before any deployment push, follow `docs/BETA_RELEASE_CHECKLIST_KO.md` and compare beta/prod runtime parity with `docs/BETA_PROD_PARITY_CHECKLIST_KO.md`.
- From the development machine, deploy with:
  - `git remote add <deploy-remote> <runtime-user>@<runtime-host>:/path/to/git/UOS_secretary.git`
  - `git add . && git commit -m "..."`  
  - `git push <deploy-remote> main:beta`
  - After validation on the beta bot, confirm `HEAD` is still the tested commit and run `git push <deploy-remote> HEAD:deploy`
- `deploy/git/redeploy.sh` reinstalls the editable package and kickstarts any already-loaded launchd jobs for that instance in either `system` or `gui/<uid>`.

## Beta Release Checklist

- Operator runbook: `docs/BETA_RELEASE_CHECKLIST_KO.md`
- Required gates before beta push: release sanitization, beta-critical tests, `doctor` + `status`, one Telegram command smoke, one `/bot` smoke, one manual briefing preview.
- Keep beta deploy (`git push <deploy-remote> HEAD:beta`) and prod promotion (`git push <deploy-remote> HEAD:deploy`) as separate steps.
- If any secret-bearing artifact was exposed before sanitizing, rotate it using `docs/ops/secret-rotation.md`.

Recommended operator smoke order:

1. `./.venv/bin/python -m sidae_secretary.cli doctor --config-file <target-config>`
2. `./.venv/bin/python -m sidae_secretary.cli uclass probe --config-file <target-config>`
3. `./.venv/bin/python -m sidae_secretary.cli status --config-file <target-config>`
4. `./.venv/bin/python -m sidae_secretary.cli ops snapshot --config-file <target-config>`
5. Compare onboarding local/public reachability for `/connect` changes:
   - local: `http://127.0.0.1:8791/...`
   - public: `ONBOARDING_PUBLIC_BASE_URL/...`
6. In Telegram, verify `/setup` first, then one of `/today` or `/status`.
7. In Telegram, verify `/bot 오늘 일정 알려줘` on beta.
8. If beta assistant writes are enabled, verify one harmless write path such as `/bot 동대문구로 날씨 지역 바꿔줘`.
9. `./.venv/bin/python -m sidae_secretary.cli publish --config-file <target-config>` and preview one generated briefing text file.

Operator points to inspect:

- `status`:
  - `health.overall_ready`
  - critical surfaces under `health.surfaces`
- `ops snapshot`:
  - `headline`
  - `instances[*].load_error`
  - `instances[*].health_summary`
  - `services.counts`
- Telegram `/setup`:
  - official API / UClass / Telegram readiness lines match the target instance
- Telegram `/today`:
  - no cross-instance cached content
  - no stale "first sync pending" or timetable disconnect regression after a healthy official API sync

## Notes

- Secrets are read from `.env` and should not be committed.
- Beta defaults to file-backed secret storage when `INSTANCE_NAME` is `beta` unless `SECRET_STORE_BACKEND` is explicitly set.
- If you switch an existing beta instance from keychain-backed secrets to file-backed secrets, reconnect each school account once so stored `secret_kind/ref` and `login_secret_kind/ref` rows are rewritten.
- After deploying the per-user UClass HTML fallback change, reconnect each existing school account once so `login_secret_kind/ref` is populated.
- If you use UClass ID/PW token refresh, prefer leaving `UCLASS_WSTOKEN=` blank so stale static tokens do not override the fresh login flow.
- For UClass auth troubleshooting, run `sidae uclass probe` first. If it succeeds, `sidae sync-uclass --wait --timeout 600` should also work.
- Path-like config settings are resolved against the selected config file directory, then normalized to absolute paths.
  This includes at least: `DATABASE_PATH`, `ICLOUD_DIR`.
- If timetable locations are in `building-room` form (example: `21-101`), load building mappings via `sidae buildings ...` to render friendly names in briefings.
- Example: after mapping seed, class lines become `14:00에 100주년 기념관 311호에서 ... 수업`.
- DB path default (before resolution) is `data/sidae.db`.
- Dashboard `index.html` is self-contained (embedded JSON) and works in `file://` contexts.
- Telegram replies, reminder dispatch, and direct briefings read the local SQLite DB directly; they do not read from iCloud Drive.
- The current beta-critical path does not require iCloud dashboard publishing, GUI control, or relay delivery.
- `sidae publish` writes precomputed Telegram briefing artifacts to:
  - `SidaeSecretary/dashboard/telegram_briefings/index.json`
  - date-slot payloads such as `SidaeSecretary/dashboard/telegram_briefings/2026-03-08-morning.json`
  - date-slot text bodies such as `SidaeSecretary/dashboard/telegram_briefings/2026-03-08-morning.txt`
- `sidae publish` is not a school-notice fetcher.
  It renders the local DB snapshot to the iCloud dashboard and writes precomputed Telegram briefing files.
- These files are based on the last successful sync/publish snapshot and are intended for an external scheduled sender.
- If the external sender should be the only scheduled briefing sender, set `BRIEFING_DELIVERY_MODE=precompute_only`.
- If `BRIEFING_RELAY_ENDPOINT` and `BRIEFING_RELAY_SHARED_SECRET` are configured, each date-slot JSON also includes a signed `relay_request` payload for an external sender.
- Recommended always-on Mac setup:
  - Mac collector: `sidae launchd install-uclass-poller --interval-minutes 60 --connectivity-check-seconds 30 --scope daemon --run-as-user <mac_user>`
  - Weather sync: `sidae launchd install-weather-sync --minute-offset 20 --sync-timeout-seconds 600 --scope daemon --run-as-user <mac_user>`
  - Telegram listener: `sidae launchd install-telegram-listener --poll-timeout-seconds 10 --error-backoff-seconds 2 --max-consecutive-errors 6 --scope daemon --run-as-user <mac_user>`
  - Onboarding server: `sidae launchd install-onboarding --host 127.0.0.1 --port 8791 --scope daemon --run-as-user <mac_user>`
  - Set `BRIEFING_DELIVERY_MODE=direct` if the Mac itself should send the 09:00 / 21:00 briefings.
  - Optional add-ons: `publish`, relay delivery
- Optional split for a Mac that is not always awake:
  - Mac collector: `sidae launchd install-uclass-poller --interval-minutes 60 --connectivity-check-seconds 30 --scope daemon --run-as-user <mac_user>`
  - Weather sync: `sidae launchd install-weather-sync --minute-offset 20 --sync-timeout-seconds 600 --scope daemon --run-as-user <mac_user>`
  - External scheduled sender: use `sidae publish` precomputed briefing artifacts or the signed relay path
  - In that case set `BRIEFING_DELIVERY_MODE=precompute_only`
- Optional delivery split:
  - Mac: `sync --all`, LLM summarization, iCloud publish, signed precompute only
  - iPhone Shortcut or another external sender: read date-slot JSON and POST `relay_request.body` to the relay endpoint at the exact send time
  - Relay host: store Telegram bot token + `BRIEFING_RELAY_SHARED_SECRET`, reject duplicate `item_key`, send via Telegram Bot API
- Optional P2 flags are available in `config.example.toml` / `.env.example`:
  - Material extraction + briefing (`MATERIAL_EXTRACTION_ENABLED`, `MATERIAL_BRIEFING_ENABLED`)
  - Material brief Telegram push (`MATERIAL_BRIEF_PUSH_ENABLED`, `MATERIAL_BRIEF_PUSH_MAX_ITEMS`)
  - Spaced review scheduler (`REVIEW_ENABLED`, `REVIEW_INTERVALS_DAYS`, `REVIEW_DURATION_MIN`)
  - Daily digest push (`DIGEST_ENABLED`, `DIGEST_TIME_LOCAL`, `DIGEST_CHANNEL`)
  - Morning/evening briefing push (`BRIEFING_ENABLED`, `BRIEFING_MORNING_TIME_LOCAL`, `BRIEFING_EVENING_TIME_LOCAL`, `BRIEFING_DELIVERY_MODE`, `BRIEFING_RELAY_ENDPOINT`, `BRIEFING_RELAY_SHARED_SECRET`, `BRIEFING_TASK_LOOKAHEAD_DAYS`)
- Notification delivery policy precedence:
  - `notification_policies` is evaluated first for `briefing_morning` (`morning_briefing` alias), `briefing_evening` (`evening_briefing` alias), `daily_digest`, and `material_brief_push`.
  - If a matching `notification_policies` row exists for that user and kind, it overrides legacy booleans in `user_preferences`.
  - If no matching policy row exists, delivery falls back to the existing `user_preferences` boolean plus the configured/connected default chat selection.
  - Prefer `days_of_week_json` values in `mon`-`sun` form. `time_local` is interpreted in the policy timezone when present, otherwise the user timezone and then the instance timezone fallback.
- Optional P3 flags:
  - Telegram command control (`TELEGRAM_COMMANDS_ENABLED`)
  - Telegram natural-language reminder planning (`TELEGRAM_SMART_COMMANDS_ENABLED`)
  - Telegram natural-language assistant entrypoint (`TELEGRAM_ASSISTANT_ENABLED`, default `false`)
  - Telegram assistant state-changing actions (`TELEGRAM_ASSISTANT_WRITE_ENABLED`, default `false`; keep off unless assistant writes are intentionally enabled)
  - Closed beta recommendation: turn both assistant flags on with `LLM_ENABLED=true`, then promote to prod only after beta `/bot` validation
  - UClass ID/PW auto-token (`UCLASS_USERNAME`, `UCLASS_PASSWORD`, `UCLASS_TOKEN_SERVICE`, `UCLASS_TOKEN_ENDPOINT`)

## Privacy Gate (`INCLUDE_IDENTITY`)

- Default: `INCLUDE_IDENTITY=false` (normal behavior).
- When `INCLUDE_IDENTITY=true`, outbound identity-sensitive steps are gated:
  - Telegram digest send
  - Telegram command replies
  - LLM summary calls
- Without ACK, these steps return a structured warning gate payload with
  `error=identity_ack_required` and no external send occurs.
- Grant ACK explicitly:
  - `sidae ack identity --expires-hours 24`
  - or `sidae ack identity --token <token> --expires-hours 24`

## Testing

Hermetic Python 3.11 workflow:

1. Create the repo venv: `python3.11 -m venv .venv`
2. Install the project and test dependencies: `./.venv/bin/python -m pip install --upgrade pip && ./.venv/bin/python -m pip install -e .[dev]`
3. Run the beta-critical suite: `./.venv/bin/python -m pytest -q -m beta_critical`
4. Run the full suite when needed: `./.venv/bin/python -m pytest -q`

Beta-critical test set:

- `tests/test_beta_critical_path.py`
  Focused fixture-driven coverage for UOS official API normalization, per-user weather, unified day-brief matching, and notice commands.
- `tests/test_p4_uos_portal_timetable.py`
  UOS official API and portal fallback timetable behavior.
- `tests/test_p3_uclass_payload_variants.py`
  UClass payload normalization for notifications, assignments, and forum notices.
- `tests/test_p4_uclass_sync_stages.py`
  UClass sync-stage behavior for the beta ingestion path.
- `tests/test_weather_kma.py`
  Hermetic KMA weather connector coverage.
- `tests/test_portal_notices.py`
  Hermetic UOS portal notice parsing coverage.

CI uses the same command in `.github/workflows/beta-critical.yml`.

## Docs Artifacts Workflow

1. Run tests: `./.venv/bin/python -m pytest -q`
2. Refresh docs artifacts: `sidae docs-artifacts sync`
3. Validate consistency (non-zero on divergence): `sidae docs-artifacts check --require-clean-git`

## Troubleshooting: `urllib3` `NotOpenSSLWarning`

- Triggering environment:
  - macOS runtime where Python `ssl` is linked to LibreSSL (for example, Apple system Python),
  - plus `urllib3>=2` import path, usually through `requests`.
- Symptom:
  - warning similar to `urllib3 v2 only supports OpenSSL 1.1.1+ ... currently the 'ssl' module is compiled with 'LibreSSL'`.
- Recommended setup:
  - Use Python from Homebrew or python.org that reports `OpenSSL` from `ssl.OPENSSL_VERSION`.
  - Recreate venv and reinstall: `rm -rf .venv && python3.11 -m venv .venv && ./.venv/bin/python -m pip install -e .[dev]`
- Temporary fallback (if runtime cannot be changed immediately):
  - pin `urllib3<2` in that environment only.
