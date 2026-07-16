"""Credential backends, non-disclosing status, and text redaction."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Protocol


_TOKEN_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}(?![A-Za-z0-9-])"),
)
_BEARER_TOKEN = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")


class CredentialBackend(Protocol):
    """Minimal storage contract consumed by ``CredentialService``."""

    source: str

    def get(self, provider: str) -> str | None: ...

    def set(self, provider: str, secret: str) -> None: ...

    def clear(self, provider: str) -> None: ...


@dataclass(frozen=True)
class CredentialStatus:
    """Credential metadata safe for CLI, Web, logs, and events."""

    provider: str
    configured: bool
    source: str


class CredentialService:
    """Expose credential mutations and non-disclosing status."""

    def __init__(self, backend: CredentialBackend) -> None:
        self._backend = backend

    def status(self, provider: str) -> CredentialStatus:
        return CredentialStatus(
            provider=provider,
            configured=self._backend.get(provider) is not None,
            source=self._backend.source,
        )

    def set(self, provider: str, secret: str) -> None:
        self._backend.set(provider, secret)

    def clear(self, provider: str) -> None:
        self._backend.clear(provider)

    def get_for_provider(self, provider: str) -> str | None:
        """Return a key only to provider composition code."""

        return self._backend.get(provider)


class KeyringBackend:
    """Store provider credentials in the operating system keyring."""

    source = "keyring"

    def __init__(self, service_name: str = "forgeloop") -> None:
        try:
            import keyring
        except ImportError as exc:
            raise RuntimeError("keyring credential backend is unavailable") from exc
        self._keyring: ModuleType = keyring
        self._service_name = service_name

    def get(self, provider: str) -> str | None:
        return self._keyring.get_password(self._service_name, provider)

    def set(self, provider: str, secret: str) -> None:
        self._keyring.set_password(self._service_name, provider, secret)

    def clear(self, provider: str) -> None:
        if self._keyring.get_password(self._service_name, provider) is not None:
            self._keyring.delete_password(self._service_name, provider)


class SecretFileBackend:
    """Read one provider credential from an owner-only regular secret file."""

    source = "secret_file"

    def __init__(self, path: str | Path, provider: str = "openai") -> None:
        self._path = Path(path)
        self._provider = provider
        descriptor = self._open_validated()
        os.close(descriptor)

    def _open_validated(self) -> int:
        try:
            path_info = self._path.lstat()
        except OSError as exc:
            raise ValueError("secret path must be a regular file") from exc
        if not stat.S_ISREG(path_info.st_mode):
            raise ValueError("secret path must be a regular file")

        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._path, flags)
        except OSError as exc:
            raise ValueError("secret path must be a regular file") from exc

        file_info = os.fstat(descriptor)
        if not stat.S_ISREG(file_info.st_mode):
            os.close(descriptor)
            raise ValueError("secret path must be a regular file")
        if file_info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            os.close(descriptor)
            raise ValueError("secret file must not grant group or other permissions")
        return descriptor

    def get(self, provider: str) -> str | None:
        if provider != self._provider:
            return None
        descriptor = self._open_validated()
        with os.fdopen(descriptor, encoding="utf-8") as secret_file:
            secret = secret_file.read().rstrip("\r\n")
        return secret or None

    def set(self, provider: str, secret: str) -> None:
        raise TypeError("secret file credentials are read-only")

    def clear(self, provider: str) -> None:
        raise TypeError("secret file credentials are read-only")


def redact(text: str, secrets: list[str] | tuple[str, ...]) -> str:
    """Mask registered secrets and common credential-shaped values in text."""

    redacted = text
    registered = sorted(
        (secret for secret in secrets if secret), key=len, reverse=True
    )
    for secret in registered:
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = _BEARER_TOKEN.sub(r"\1[REDACTED]", redacted)
    for pattern in _TOKEN_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted
