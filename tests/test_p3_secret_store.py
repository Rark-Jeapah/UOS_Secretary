from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sidae_secretary import secret_store as secret_store_module
from sidae_secretary.secret_store import FileSecretStore, StoredSecretRef, default_secret_store


class _FakeKeychainStore(secret_store_module.SecretStore):
    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def store_secret(self, *, key: str, secret: str) -> StoredSecretRef:
        self.items[str(key)] = str(secret)
        return StoredSecretRef(kind="keychain", ref=str(key))

    def read_secret(self, *, ref: StoredSecretRef) -> str:
        return self.items[str(ref.ref)]


def _settings(tmp_path: Path, **overrides: object) -> SimpleNamespace:
    values = {
        "database_path": tmp_path / "data" / "sidae.db",
        "storage_root_dir": None,
        "instance_name": "",
        "secret_store_backend": "",
        "secret_store_allow_file_fallback": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_default_secret_store_prefers_keychain_on_macos(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_keychain = _FakeKeychainStore()
    settings = _settings(tmp_path)
    secret_payload = '{"cookies":[{"name":"JSESSIONID","value":"abc"}]}'

    monkeypatch.setattr(secret_store_module.sys, "platform", "darwin")
    monkeypatch.setattr(secret_store_module.shutil, "which", lambda name: "/usr/bin/security")
    monkeypatch.setattr(
        secret_store_module,
        "MacOSKeychainSecretStore",
        lambda service_name="com.sidae.secretary.moodle": fake_keychain,
    )

    store = default_secret_store(settings)
    ref = store.store_secret(
        key="telegram:12345:portal:uos_portal",
        secret=secret_payload,
    )

    assert ref.kind == "keychain"
    assert fake_keychain.items["telegram:12345:portal:uos_portal"] == secret_payload
    assert store.read_secret(ref=ref) == secret_payload
    assert not (tmp_path / "data" / "secret_store").exists()


def test_default_secret_store_uses_file_fallback_when_keychain_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path, secret_store_allow_file_fallback=True)

    monkeypatch.setattr(secret_store_module.sys, "platform", "darwin")
    monkeypatch.setattr(secret_store_module.shutil, "which", lambda name: None)

    store = default_secret_store(settings)
    ref = store.store_secret(
        key="telegram:12345:moodle:uos_online_class",
        secret="uos-issued-token",
    )

    assert ref.kind == "file"
    assert store.read_secret(ref=ref) == "uos-issued-token"
    assert (tmp_path / "data" / "secret_store").exists()


def test_default_secret_store_prefers_file_for_beta_instance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_keychain = _FakeKeychainStore()
    settings = _settings(tmp_path, instance_name="beta")

    monkeypatch.setattr(secret_store_module.sys, "platform", "darwin")
    monkeypatch.setattr(secret_store_module.shutil, "which", lambda name: "/usr/bin/security")
    monkeypatch.setattr(
        secret_store_module,
        "MacOSKeychainSecretStore",
        lambda service_name="com.sidae.secretary.moodle": fake_keychain,
    )

    store = default_secret_store(settings)
    ref = store.store_secret(
        key="telegram:12345:moodle:uos_online_class",
        secret="uos-issued-token",
    )

    assert ref.kind == "file"
    assert store.read_secret(ref=ref) == "uos-issued-token"
    assert fake_keychain.items == {}
    assert (tmp_path / "data" / "secret_store").exists()


def test_default_secret_store_reads_legacy_file_secret_on_macos(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_keychain = _FakeKeychainStore()
    settings = _settings(tmp_path)
    legacy_store = FileSecretStore(tmp_path / "data" / "secret_store")
    legacy_ref = legacy_store.store_secret(
        key="telegram:12345:moodle:uos_online_class",
        secret="legacy-token",
    )

    monkeypatch.setattr(secret_store_module.sys, "platform", "darwin")
    monkeypatch.setattr(secret_store_module.shutil, "which", lambda name: "/usr/bin/security")
    monkeypatch.setattr(
        secret_store_module,
        "MacOSKeychainSecretStore",
        lambda service_name="com.sidae.secretary.moodle": fake_keychain,
    )

    store = default_secret_store(settings)

    assert legacy_ref.kind == "file"
    assert store.read_secret(ref=legacy_ref) == "legacy-token"
