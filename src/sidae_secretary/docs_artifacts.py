from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
from typing import Any


GENERATED_AT_PATTERN = re.compile(r"^(Generated at \(UTC\): )`[^`]+`$", re.MULTILINE)
SNAPSHOT_BRANCH_PATTERN = re.compile(r"^(- Branch: )`[^`]+`$", re.MULTILINE)
SNAPSHOT_HEAD_PATTERN = re.compile(r"^(- HEAD: )`[^`]+`$", re.MULTILINE)
SNAPSHOT_DIRTY_PATTERN = re.compile(r"^(- Working tree dirty: )`[^`]+`.*$", re.MULTILINE)
AUDIT_HEAD_PATTERN = re.compile(
    r"^(- Current HEAD captured: )`[^`]+` on `[^`]+`\.$",
    re.MULTILINE,
)
AUDIT_DIRTY_PATTERN = re.compile(r"^(- Working tree dirty: )`[^`]+`.*$", re.MULTILINE)
MARKDOWN_GENERATED_AT_READ_PATTERN = re.compile(
    r"^Generated at \(UTC\): `([^`]+)`$",
    re.MULTILINE,
)


@dataclass
class GitMetadata:
    branch: str
    head: str
    dirty: bool
    dirty_files: list[str]

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "branch": self.branch,
            "head": self.head,
            "dirty": self.dirty,
        }
        if self.dirty:
            payload["dirty_files"] = list(self.dirty_files)
        return payload


def _now_utc_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_git_command(repo_root: Path, args: list[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root)] + args,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.rstrip("\n")


def parse_git_status_porcelain(output: str) -> list[str]:
    paths: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if len(line) < 4:
            continue
        path_part = line[3:].strip()
        if not path_part:
            continue
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1].strip()
        if path_part.startswith('"') and path_part.endswith('"') and len(path_part) >= 2:
            path_part = path_part[1:-1]
        if path_part:
            paths.append(path_part)
    return sorted(set(paths))


def collect_git_metadata(repo_root: Path) -> GitMetadata:
    branch = _run_git_command(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    head = _run_git_command(repo_root, ["rev-parse", "HEAD"]).strip()
    status_output = _run_git_command(repo_root, ["status", "--porcelain"])
    dirty_files = parse_git_status_porcelain(status_output)
    return GitMetadata(
        branch=branch,
        head=head,
        dirty=bool(dirty_files),
        dirty_files=dirty_files,
    )


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected object JSON: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _sync_git_dict(target: dict[str, Any], metadata: GitMetadata) -> None:
    target["branch"] = metadata.branch
    target["head"] = metadata.head
    target["dirty"] = metadata.dirty
    if metadata.dirty:
        target["dirty_files"] = list(metadata.dirty_files)
    else:
        target.pop("dirty_files", None)


def _sync_snapshot_payload(payload: dict[str, Any], generated_at: str, metadata: GitMetadata) -> dict[str, Any]:
    payload["generated_at"] = generated_at
    repo_section = payload.get("repo")
    if not isinstance(repo_section, dict):
        repo_section = {}
        payload["repo"] = repo_section
    git_section = repo_section.get("git")
    if not isinstance(git_section, dict):
        git_section = {}
        repo_section["git"] = git_section
    _sync_git_dict(git_section, metadata)
    return payload


def _sync_audit_payload(payload: dict[str, Any], generated_at: str, metadata: GitMetadata) -> dict[str, Any]:
    payload["generated_at"] = generated_at
    git_section = payload.get("git")
    if not isinstance(git_section, dict):
        git_section = {}
        payload["git"] = git_section
    _sync_git_dict(git_section, metadata)
    return payload


def _sync_snapshot_markdown(text: str, generated_at: str, metadata: GitMetadata) -> str:
    dirty_line = f"- Working tree dirty: `{'true' if metadata.dirty else 'false'}`"
    if metadata.dirty:
        dirty_line += f" (dirty files: {len(metadata.dirty_files)})"
    output = GENERATED_AT_PATTERN.sub(rf"\1`{generated_at}`", text)
    output = SNAPSHOT_BRANCH_PATTERN.sub(rf"\1`{metadata.branch}`", output)
    output = SNAPSHOT_HEAD_PATTERN.sub(rf"\1`{metadata.head}`", output)
    output = SNAPSHOT_DIRTY_PATTERN.sub(dirty_line, output)
    return output


def _sync_audit_markdown(text: str, generated_at: str, metadata: GitMetadata) -> str:
    dirty_line = f"- Working tree dirty: `{'true' if metadata.dirty else 'false'}`"
    if metadata.dirty:
        dirty_line += f" (dirty files: {len(metadata.dirty_files)})"
    output = GENERATED_AT_PATTERN.sub(rf"\1`{generated_at}`", text)
    output = AUDIT_HEAD_PATTERN.sub(
        rf"\1`{metadata.head}` on `{metadata.branch}`.",
        output,
    )
    if AUDIT_DIRTY_PATTERN.search(output):
        output = AUDIT_DIRTY_PATTERN.sub(dirty_line, output)
    elif AUDIT_HEAD_PATTERN.search(output):
        output = AUDIT_HEAD_PATTERN.sub(
            rf"\1`{metadata.head}` on `{metadata.branch}`.\n{dirty_line}",
            output,
        )
    return output


def _extract_markdown_generated_at(text: str) -> str | None:
    match = MARKDOWN_GENERATED_AT_READ_PATTERN.search(text)
    if not match:
        return None
    return str(match.group(1)).strip()


def check_docs_artifacts_consistency(
    docs_dir: Path,
    repo_root: Path,
    require_clean_git: bool = False,
) -> dict[str, Any]:
    snapshot_json_path = docs_dir / "snapshot.json"
    audit_json_path = docs_dir / "audit.json"
    snapshot_md_path = docs_dir / "SNAPSHOT.md"
    audit_md_path = docs_dir / "AUDIT.md"

    metadata = collect_git_metadata(repo_root=repo_root)
    snapshot_json = _read_json(snapshot_json_path)
    audit_json = _read_json(audit_json_path)
    snapshot_md = snapshot_md_path.read_text(encoding="utf-8")
    audit_md = audit_md_path.read_text(encoding="utf-8")

    errors: list[str] = []
    if require_clean_git and metadata.dirty:
        errors.append("git working tree is dirty")

    snapshot_git = (
        snapshot_json.get("repo", {}).get("git", {})
        if isinstance(snapshot_json.get("repo"), dict)
        else {}
    )
    audit_git = audit_json.get("git", {}) if isinstance(audit_json.get("git"), dict) else {}

    for key in ("branch", "head", "dirty"):
        snapshot_value = snapshot_git.get(key)
        audit_value = audit_git.get(key)
        expected = getattr(metadata, key)
        if snapshot_value != audit_value:
            errors.append(f"snapshot/audit mismatch for git.{key}")
        if snapshot_value != expected:
            errors.append(f"snapshot git.{key} does not match current git state")
        if audit_value != expected:
            errors.append(f"audit git.{key} does not match current git state")

    if metadata.dirty:
        snapshot_dirty_files = sorted(
            [str(item) for item in snapshot_git.get("dirty_files", [])]
            if isinstance(snapshot_git.get("dirty_files"), list)
            else []
        )
        audit_dirty_files = sorted(
            [str(item) for item in audit_git.get("dirty_files", [])]
            if isinstance(audit_git.get("dirty_files"), list)
            else []
        )
        expected_dirty_files = sorted(metadata.dirty_files)
        if snapshot_dirty_files != expected_dirty_files:
            errors.append("snapshot dirty_files does not match git status --porcelain")
        if audit_dirty_files != expected_dirty_files:
            errors.append("audit dirty_files does not match git status --porcelain")

    generated_values = {
        "snapshot.json": snapshot_json.get("generated_at"),
        "audit.json": audit_json.get("generated_at"),
        "SNAPSHOT.md": _extract_markdown_generated_at(snapshot_md),
        "AUDIT.md": _extract_markdown_generated_at(audit_md),
    }
    non_empty_generated = [str(item).strip() for item in generated_values.values() if item]
    if len(set(non_empty_generated)) > 1:
        errors.append("generated_at does not match across artifacts")

    return {
        "ok": not errors,
        "errors": errors,
        "git": metadata.as_dict(),
        "generated_at": generated_values,
    }


def sync_docs_artifacts(
    docs_dir: Path,
    repo_root: Path,
    require_clean_git: bool = False,
    generated_at: str | None = None,
) -> dict[str, Any]:
    metadata = collect_git_metadata(repo_root=repo_root)
    if require_clean_git and metadata.dirty:
        raise RuntimeError("git working tree is dirty")

    snapshot_json_path = docs_dir / "snapshot.json"
    audit_json_path = docs_dir / "audit.json"
    snapshot_md_path = docs_dir / "SNAPSHOT.md"
    audit_md_path = docs_dir / "AUDIT.md"

    generated = generated_at.strip() if generated_at and generated_at.strip() else _now_utc_z()
    snapshot_payload = _sync_snapshot_payload(_read_json(snapshot_json_path), generated, metadata)
    audit_payload = _sync_audit_payload(_read_json(audit_json_path), generated, metadata)
    snapshot_md = _sync_snapshot_markdown(
        snapshot_md_path.read_text(encoding="utf-8"),
        generated_at=generated,
        metadata=metadata,
    )
    audit_md = _sync_audit_markdown(
        audit_md_path.read_text(encoding="utf-8"),
        generated_at=generated,
        metadata=metadata,
    )

    _write_json(snapshot_json_path, snapshot_payload)
    _write_json(audit_json_path, audit_payload)
    snapshot_md_path.write_text(snapshot_md, encoding="utf-8")
    audit_md_path.write_text(audit_md, encoding="utf-8")

    check_result = check_docs_artifacts_consistency(
        docs_dir=docs_dir,
        repo_root=repo_root,
        require_clean_git=require_clean_git,
    )
    return {
        "ok": bool(check_result.get("ok")),
        "generated_at": generated,
        "git": metadata.as_dict(),
        "updated_files": [
            str(snapshot_json_path),
            str(audit_json_path),
            str(snapshot_md_path),
            str(audit_md_path),
        ],
        "check": check_result,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sidae_secretary.docs_artifacts",
        description="Sync/validate docs snapshot and audit artifact metadata.",
    )
    parser.add_argument("--docs-dir", default="docs", help="Docs directory path.")
    parser.add_argument("--repo-root", default=".", help="Git repository root path.")
    parser.add_argument(
        "--require-clean-git",
        action="store_true",
        help="Fail when git working tree has local changes.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only validate artifacts; do not modify files.",
    )
    parser.add_argument(
        "--generated-at",
        default="",
        help="Override generated_at timestamp when syncing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    docs_dir = Path(str(args.docs_dir)).expanduser().resolve()
    repo_root = Path(str(args.repo_root)).expanduser().resolve()
    try:
        if args.check:
            result = check_docs_artifacts_consistency(
                docs_dir=docs_dir,
                repo_root=repo_root,
                require_clean_git=bool(args.require_clean_git),
            )
        else:
            result = sync_docs_artifacts(
                docs_dir=docs_dir,
                repo_root=repo_root,
                require_clean_git=bool(args.require_clean_git),
                generated_at=str(args.generated_at or "").strip() or None,
            )
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=True,
                indent=2,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if bool(result.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
