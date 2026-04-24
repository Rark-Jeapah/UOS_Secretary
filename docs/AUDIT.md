# Project Audit (`UOS_secretary`)

Generated at (UTC): `2026-03-05T11:19:17Z`

## Executive Summary
- Local audit run is green: `99 passed, 1 warning in 2.06s` via `python3 -m pytest -q`.
- Current HEAD captured: `a3f4cfab7dca72ce884233b99d2b61cfaf3e823e` on `main`.
- Working tree dirty: `true` (dirty files: 21)
- Path determinism, launchd hardening, privacy ACK gating, runtime checks, and docs workflow updates are implemented.

## Findings
1. Config path resolution now prevents split-brain DB/lock behavior.
- Relative `DATABASE_PATH` and `ICLOUD_DIR` resolve from the selected config directory.
- Resolved settings are absolute regardless of launch context.

2. launchd execution is hardened against PATH/venv drift.
- Generated plist uses `sys.executable -m sidae_secretary.cli`.
- `--config-file` is always absolute.
- `WorkingDirectory` is pinned to config parent.

3. include_identity warning-gate and ACK flow added.
- New config flag: `INCLUDE_IDENTITY` (default `false`).
- When enabled, outbound Telegram/LLM steps block with structured `identity_ack_required` payload unless ACK exists.
- Human ACK is persisted via `sidae ack identity --token ... --expires-hours N`.

4. doctor now performs explicit runtime environment checks.
- Fails with clear message when Python is below `3.11`.
- Reports SSL backend and warns when runtime uses LibreSSL.
- Runtime checks are exposed in a stable `runtime` report key.

5. Audit/snapshot artifacts are synchronized with current run evidence.
- `docs/audit.json`, `docs/AUDIT.md`, `docs/snapshot.json`, `docs/SNAPSHOT.md` share one `generated_at`, HEAD, and pytest totals.

## Test Evidence (Current Run)
- Command: `python3 -m pytest -q`
- Result: `99 passed, 1 warning in 2.06s`
- Added/updated coverage:
  - `tests/test_p0_config_path_resolution.py`
  - `tests/test_p1_ack_identity_cli.py`
  - `tests/test_p1_identity_gate.py`
  - `tests/test_p2_docs_artifacts_cli.py`
  - `tests/test_p2_doctor_runtime_checks.py`
  - `tests/test_p3_launchd_install.py`

## Residual Risk
- Environment warning persists when Python is linked against LibreSSL: `urllib3` emits `NotOpenSSLWarning`.
