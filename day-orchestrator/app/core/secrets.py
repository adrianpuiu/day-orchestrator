"""Secret store: macOS Keychain first, .env fallback, never logs values (C8).

Lifted interface from the long-horizon-harness recipe: a small backend protocol
so tests use an in-memory store and the model never sees a secret — only names.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

SERVICE = "day-orchestrator"


class SecretBackend(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...


class KeychainBackend:
    """macOS Keychain via the `keyring` library."""

    def get(self, name: str) -> str | None:
        import keyring

        try:
            return keyring.get_password(SERVICE, name)
        except keyring.errors.KeyRingError:
            return None

    def set(self, name: str, value: str) -> None:
        import keyring

        keyring.set_password(SERVICE, name, value)


class EnvFileBackend:
    """Simple KEY=VALUE .env reader/writer (fallback when Keychain unavailable)."""

    def __init__(self, env_file: Path) -> None:
        self._path = env_file

    def _read(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    out[k.strip()] = v.strip()
        return out

    def get(self, name: str) -> str | None:
        return self._read().get(name)

    def set(self, name: str, value: str) -> None:
        data = self._read()
        data[name] = value
        lines = [f"{k}={v}" for k, v in data.items()]
        self._path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class InMemoryBackend:
    """Test double."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self._data.get(name)

    def set(self, name: str, value: str) -> None:
        self._data[name] = value


class Secrets:
    def __init__(self, *backends: SecretBackend) -> None:
        self._backends = backends

    def get(self, name: str) -> str | None:
        for backend in self._backends:
            value = backend.get(name)
            if value and not value.startswith(("your-", "changeme")):
                return value
        return os.environ.get(name)

    def require(self, name: str) -> str:
        value = self.get(name)
        if not value:
            raise KeyError(
                f"Missing secret {name!r}. Run: uv run python -m app.cli set-secret {name}"
            )
        return value


def default_secrets(project_root: Path) -> Secrets:
    return Secrets(KeychainBackend(), EnvFileBackend(project_root / ".env"))
