# Repository Snapshot (`UOS_secretary`)

Generated at (UTC): `2026-03-05T11:19:17Z`

## Git State
- Branch: `main`
- HEAD: `a3f4cfab7dca72ce884233b99d2b61cfaf3e823e`
- Working tree dirty: `true` (dirty files: 21)

## Test Status
- Command: `python3 -m pytest -q`
- Result: `99 passed, 1 warning in 2.06s`
- Warning: `urllib3 NotOpenSSLWarning` on this local Python runtime.

## Verified Command/Behavior Snapshot
- `load_settings`
  - Resolves relative path-like settings against the selected config file directory.
  - Produces absolute paths for DB and iCloud root.
- `sidae launchd install`
  - Pins `ProgramArguments` to `sys.executable -m sidae_secretary.cli ...`.
  - Always writes absolute `--config-file`.
  - Sets `WorkingDirectory` to the resolved config directory.
- `INCLUDE_IDENTITY` privacy gate
  - `false`: normal outbound behavior.
  - `true`: outbound Telegram/LLM steps block unless ACK exists.
  - Blocked responses include machine-readable `identity_ack_required` warning-gate payload.
- `sidae doctor`
  - Includes runtime checks for Python version and SSL backend.
  - Fails with code `2` when Python version is below `3.11`.

## Updated/Added Tests in This Revision
- `tests/test_p0_config_path_resolution.py`
- `tests/test_p1_ack_identity_cli.py`
- `tests/test_p1_identity_gate.py`
- `tests/test_p2_docs_artifacts_cli.py`
- `tests/test_p2_doctor_runtime_checks.py`
- `tests/test_p3_launchd_install.py`
