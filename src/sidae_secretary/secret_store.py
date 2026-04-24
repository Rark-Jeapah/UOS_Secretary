from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


class SecretStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredSecretRef:
    kind: str
    ref: str


class SecretStore:
    def store_secret(self, *, key: str, secret: str) -> StoredSecretRef:
        raise NotImplementedError

    def read_secret(self, *, ref: StoredSecretRef) -> str:
        raise NotImplementedError


class UnavailableSecretStore(SecretStore):
    def __init__(self, message: str) -> None:
        self.message = str(message).strip() or "secret store is unavailable"

    def store_secret(self, *, key: str, secret: str) -> StoredSecretRef:
        raise SecretStoreError(self.message)

    def read_secret(self, *, ref: StoredSecretRef) -> str:
        raise SecretStoreError(self.message)


class InlineSecretStore(SecretStore):
    def store_secret(self, *, key: str, secret: str) -> StoredSecretRef:
        return StoredSecretRef(kind="inline", ref=str(secret))

    def read_secret(self, *, ref: StoredSecretRef) -> str:
        return str(ref.ref)


class FileSecretStore(SecretStore):
    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir).expanduser()

    def _filename_for_key(self, key: str) -> str:
        return sha256(str(key or "").strip().encode("utf-8")).hexdigest() + ".secret"

    def ref_for_key(self, key: str) -> StoredSecretRef:
        return StoredSecretRef(kind="file", ref=self._filename_for_key(key))

    def _path_for_ref(self, ref: StoredSecretRef) -> Path:
        name = Path(str(ref.ref or "").strip()).name
        if not name:
            raise SecretStoreError("secret ref is required")
        return self.base_dir / name

    def store_secret(self, *, key: str, secret: str) -> StoredSecretRef:
        normalized_key = str(key or "").strip()
        if not normalized_key:
            raise SecretStoreError("secret key is required")
        ref = self.ref_for_key(normalized_key)
        path = self._path_for_ref(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(str(secret), encoding="utf-8")
        try:
            os.chmod(tmp_path, 0o600)
        except Exception:
            pass
        tmp_path.replace(path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return ref

    def read_secret(self, *, ref: StoredSecretRef) -> str:
        path = self._path_for_ref(ref)
        if not path.exists():
            raise SecretStoreError("stored secret file is missing")
        return path.read_text(encoding="utf-8")


class MacOSKeychainSecretStore(SecretStore):
    def __init__(self, service_name: str = "com.sidae.secretary.moodle") -> None:
        self.service_name = str(service_name or "com.sidae.secretary.moodle").strip()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["security", *args],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = str(exc.stderr or "").strip()
            raise SecretStoreError(stderr or "security command failed") from exc

    def store_secret(self, *, key: str, secret: str) -> StoredSecretRef:
        account = str(key or "").strip()
        if not account:
            raise SecretStoreError("secret key is required")
        self._run(
            "add-generic-password",
            "-U",
            "-a",
            account,
            "-s",
            self.service_name,
            "-w",
            str(secret),
        )
        return StoredSecretRef(kind="keychain", ref=account)

    def read_secret(self, *, ref: StoredSecretRef) -> str:
        account = str(ref.ref or "").strip()
        if not account:
            raise SecretStoreError("secret ref is required")
        result = self._run(
            "find-generic-password",
            "-a",
            account,
            "-s",
            self.service_name,
            "-w",
        )
        return str(result.stdout or "").strip()


class RoutedSecretStore(SecretStore):
    def __init__(
        self,
        *,
        preferred_store: SecretStore,
        inline_store: SecretStore | None = None,
        keychain_store: SecretStore | None = None,
        file_store: FileSecretStore | None = None,
        allow_file_fallback: bool = False,
    ) -> None:
        self.preferred_store = preferred_store
        self.inline_store = inline_store or InlineSecretStore()
        self.keychain_store = keychain_store
        self.file_store = file_store
        self.allow_file_fallback = bool(allow_file_fallback)

    def store_secret(self, *, key: str, secret: str) -> StoredSecretRef:
        try:
            return self.preferred_store.store_secret(key=key, secret=secret)
        except SecretStoreError:
            if not self.allow_file_fallback or self.file_store is None:
                raise
            if self.preferred_store is self.file_store:
                raise
            return self.file_store.store_secret(key=key, secret=secret)

    def read_secret(self, *, ref: StoredSecretRef) -> str:
        kind = str(ref.kind or "").strip().lower()
        if kind == "inline":
            return self.inline_store.read_secret(ref=ref)
        if kind == "file":
            if self.file_store is None:
                raise SecretStoreError("file secret store is not configured")
            return self.file_store.read_secret(ref=ref)
        if kind == "keychain":
            if self.keychain_store is not None:
                try:
                    return self.keychain_store.read_secret(ref=ref)
                except SecretStoreError:
                    if not self.allow_file_fallback or self.file_store is None:
                        raise
            elif not self.allow_file_fallback or self.file_store is None:
                raise SecretStoreError("macOS keychain is not configured")
            fallback_ref = self.file_store.ref_for_key(str(ref.ref or "").strip())
            return self.file_store.read_secret(ref=fallback_ref)
        raise SecretStoreError(f"unsupported secret ref kind: {kind or 'unknown'}")


def _default_secret_store_dir(settings: Any | None = None) -> Path:
    database_path = getattr(settings, "database_path", None)
    if database_path:
        return Path(database_path).expanduser().resolve().parent / "secret_store"
    storage_root_dir = getattr(settings, "storage_root_dir", None)
    if storage_root_dir:
        return Path(storage_root_dir).expanduser().resolve() / "secret_store"
    return (Path.cwd() / "data" / "secret_store").resolve()


def _configured_secret_store_backend(settings: Any | None = None) -> str:
    configured = str(getattr(settings, "secret_store_backend", "") or "").strip().lower()
    if configured in {"file", "keychain"}:
        return configured
    return ""


def _instance_prefers_file_secret_store(settings: Any | None = None) -> bool:
    instance_name = str(getattr(settings, "instance_name", "") or "").strip().lower()
    return bool(instance_name) and (
        instance_name == "beta" or instance_name.startswith("beta-")
    )


def _secret_store_file_fallback_enabled(settings: Any | None = None) -> bool:
    value = getattr(settings, "secret_store_allow_file_fallback", False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _keychain_available() -> bool:
    return sys.platform == "darwin" and bool(shutil.which("security"))


def secret_store_report(settings: Any | None = None) -> dict[str, Any]:
    configured_backend = _configured_secret_store_backend(settings)
    if configured_backend:
        preferred_backend = configured_backend
    elif _instance_prefers_file_secret_store(settings):
        preferred_backend = "file"
    else:
        preferred_backend = "keychain" if sys.platform == "darwin" else "file"
    keychain_available = _keychain_available()
    file_fallback_enabled = _secret_store_file_fallback_enabled(settings)
    if preferred_backend == "keychain":
        if keychain_available:
            active_backend = "keychain"
            write_ready = True
        elif file_fallback_enabled:
            active_backend = "file"
            write_ready = True
        else:
            active_backend = "unavailable"
            write_ready = False
    else:
        active_backend = "file"
        write_ready = True
    return {
        "configured_backend": configured_backend or "default",
        "preferred_backend": preferred_backend,
        "active_backend": active_backend,
        "keychain_available": keychain_available,
        "file_fallback_enabled": file_fallback_enabled,
        "legacy_file_read_compat": True,
        "write_ready": write_ready,
    }


def default_secret_store(settings: Any | None = None) -> SecretStore:
    report = secret_store_report(settings)
    file_store = FileSecretStore(_default_secret_store_dir(settings))
    keychain_store: SecretStore | None = None
    if report["keychain_available"]:
        keychain_store = MacOSKeychainSecretStore()
    preferred_store: SecretStore
    if report["active_backend"] == "keychain" and keychain_store is not None:
        preferred_store = keychain_store
    elif report["active_backend"] == "file":
        preferred_store = file_store
    else:
        preferred_store = UnavailableSecretStore(
            "macOS Keychain is unavailable; set SECRET_STORE_BACKEND=file or "
            "SECRET_STORE_ALLOW_FILE_FALLBACK=true to enable file-backed secrets"
        )
    return RoutedSecretStore(
        preferred_store=preferred_store,
        inline_store=InlineSecretStore(),
        keychain_store=keychain_store,
        file_store=file_store,
        allow_file_fallback=report["file_fallback_enabled"],
    )
