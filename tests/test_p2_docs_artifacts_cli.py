from __future__ import annotations

from typer.testing import CliRunner

from sidae_secretary import cli


def test_docs_artifacts_check_operation_returns_nonzero_when_inconsistent(monkeypatch) -> None:
    runner = CliRunner()

    monkeypatch.setattr(
        cli.docs_artifacts_module,
        "check_docs_artifacts_consistency",
        lambda docs_dir, repo_root, require_clean_git=False: {
            "ok": False,
            "errors": ["generated_at mismatch"],
        },
    )

    result = runner.invoke(cli.app, ["docs-artifacts", "check"])

    assert result.exit_code == 1
    assert '"ok": false' in result.stdout.lower()


def test_docs_artifacts_sync_operation_invokes_sync(monkeypatch) -> None:
    runner = CliRunner()

    monkeypatch.setattr(
        cli.docs_artifacts_module,
        "sync_docs_artifacts",
        lambda docs_dir, repo_root, generated_at=None, require_clean_git=False: {
            "ok": True,
            "updated_files": ["docs/snapshot.json"],
        },
    )

    result = runner.invoke(cli.app, ["docs-artifacts", "sync"])

    assert result.exit_code == 0
    assert '"ok": true' in result.stdout.lower()
