"""Secret store: git-ignored .env is canonical, Keychain fallback (C8).

Owner decision 2026-11-22: credentials live in `.env` (never committed) so
they are visible and migratable; Keychain remains a fallback for setups that
prefer it. Precedence order below IS the contract — do not reorder casually.
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

    def set(self, name: str, value: str) -> None:
        """Writes the canonical (first) backend; later backends are fallbacks."""
        self._backends[0].set(name, value)

    def require(self, name: str) -> str:
        value = self.get(name)
        if not value:
            raise KeyError(
                f"Missing secret {name!r}. Run: uv run python -m app.cli set-secret {name}"
            )
        return value


def default_secrets(project_root: Path) -> Secrets:
    return Secrets(EnvFileBackend(project_root / ".env"), KeychainBackend())
