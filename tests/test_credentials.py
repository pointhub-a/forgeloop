import os
import sys
from types import SimpleNamespace

import pytest

from forgeloop.credentials import (
    CredentialService,
    KeyringBackend,
    SecretFileBackend,
    redact,
)


class FakeCredentialBackend:
    source = "memory"

    def __init__(self):
        self.values = {}

    def get(self, provider):
        return self.values.get(provider)

    def set(self, provider, secret):
        self.values[provider] = secret

    def clear(self, provider):
        self.values.pop(provider, None)


@pytest.fixture
def fake_backend():
    return FakeCredentialBackend()


def test_status_and_repr_never_reveal_secret(fake_backend):
    service = CredentialService(fake_backend)
    service.set("openai", "sk-example-secret")

    status = service.status("openai")

    assert status.configured
    assert status.provider == "openai"
    assert status.source == "memory"
    assert "sk-example-secret" not in repr(status)


def test_service_gets_for_provider_and_clears_credential(fake_backend):
    service = CredentialService(fake_backend)
    service.set("openai", "sk-example-secret")

    assert service.get_for_provider("openai") == "sk-example-secret"

    service.clear("openai")

    assert service.get_for_provider("openai") is None
    assert service.status("openai").configured is False


@pytest.mark.parametrize("secret", ["", " ", "\t\r\n"])
def test_service_rejects_empty_or_whitespace_credential(fake_backend, secret):
    service = CredentialService(fake_backend)

    with pytest.raises(ValueError, match="empty"):
        service.set("openai", secret)

    assert fake_backend.values == {}
    assert service.get_for_provider("openai") is None
    assert service.status("openai").configured is False


def test_service_treats_backend_whitespace_as_unconfigured(fake_backend):
    fake_backend.values["openai"] = " \t"
    service = CredentialService(fake_backend)

    assert service.get_for_provider("openai") is None
    assert service.status("openai").configured is False


def test_redact_masks_registered_and_token_shaped_values():
    text = redact(
        "Authorization: Bearer sk-example-secret; fallback sk-unregistered-token",
        ["sk-example-secret"],
    )

    assert "secret" not in text
    assert "sk-unregistered-token" not in text
    assert "[REDACTED]" in text


def test_redact_ignores_empty_registered_secret():
    assert redact("ordinary output", [""]) == "ordinary output"


def test_secret_file_backend_reads_owner_only_regular_file(tmp_path):
    path = tmp_path / "provider-secret"
    path.write_text("sk-file-secret\n", encoding="utf-8")
    path.chmod(0o600)
    service = CredentialService(SecretFileBackend(path))

    assert service.status("openai").configured is True
    assert service.status("openai").source == "secret_file"
    assert service.get_for_provider("openai") == "sk-file-secret"
    assert service.get_for_provider("other") is None


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o666])
def test_secret_file_backend_rejects_group_or_other_permissions(tmp_path, mode):
    path = tmp_path / "provider-secret"
    path.write_text("sk-file-secret", encoding="utf-8")
    path.chmod(mode)

    with pytest.raises(ValueError, match="permission"):
        SecretFileBackend(path)


def test_secret_file_backend_rejects_non_regular_file(tmp_path):
    path = tmp_path / "provider-secret"
    path.mkdir()

    with pytest.raises(ValueError, match="regular file"):
        SecretFileBackend(path)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported")
def test_secret_file_backend_rejects_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("sk-file-secret", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "provider-secret"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="regular file"):
        SecretFileBackend(link)


def test_secret_file_backend_is_read_only(tmp_path):
    path = tmp_path / "provider-secret"
    path.write_text("sk-file-secret", encoding="utf-8")
    path.chmod(0o600)
    service = CredentialService(SecretFileBackend(path))

    with pytest.raises(TypeError, match="read-only"):
        service.set("openai", "replacement")
    with pytest.raises(TypeError, match="read-only"):
        service.clear("openai")


def test_keyring_backend_delegates_to_keyring_library(monkeypatch):
    values = {}

    def get_password(service_name, provider):
        return values.get((service_name, provider))

    def set_password(service_name, provider, secret):
        values[(service_name, provider)] = secret

    def delete_password(service_name, provider):
        values.pop((service_name, provider), None)

    fake_keyring = SimpleNamespace(
        get_password=get_password,
        set_password=set_password,
        delete_password=delete_password,
    )
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
    service = CredentialService(KeyringBackend(service_name="test-forgeloop"))

    service.set("openai", "sk-example-secret")
    assert service.status("openai").configured is True
    assert service.get_for_provider("openai") == "sk-example-secret"

    service.clear("openai")
    assert service.status("openai").configured is False
