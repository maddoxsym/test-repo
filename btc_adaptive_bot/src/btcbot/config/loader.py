"""Configuration loading: YAML inheritance, env credentials, validation, hashing.

Loading is deliberately strict. A misspelt key raises rather than being ignored,
because a silently-dropped setting during a 14-day unattended experiment is worse
than a startup crash.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from ..utils.errors import ConfigError, CredentialsMissingError
from ..utils.ids import config_hash
from ..utils.logging import register_secret
from .schema import AppConfig

MAX_EXTENDS_DEPTH = 5


@dataclass(frozen=True, slots=True)
class Credentials:
    """Demo API credentials read from the environment. Never persisted.

    OKX keys have three parts: the key, the secret, and the passphrase chosen
    when the key was created. All three are required to authenticate.
    """

    api_key: str
    api_secret: str
    passphrase: str

    def __repr__(self) -> str:  # pragma: no cover - defensive against accidental logging
        return "Credentials(api_key='***', api_secret='***', passphrase='***')"


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    """A validated config plus the provenance needed to record an experiment."""

    config: AppConfig
    config_hash: str
    source_path: Path
    raw: dict[str, Any]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base`` (override wins on scalars/lists)."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"configuration file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return data


def _resolve_with_extends(path: Path, _depth: int = 0) -> dict[str, Any]:
    """Load a YAML file, resolving its ``extends:`` chain relative to its own directory."""
    if _depth > MAX_EXTENDS_DEPTH:
        raise ConfigError(
            f"configuration 'extends' chain deeper than {MAX_EXTENDS_DEPTH} — is there a cycle?"
        )
    data = _read_yaml(path)
    parent_name = data.pop("extends", None)
    if parent_name is None:
        return data
    if not isinstance(parent_name, str):
        raise ConfigError(f"{path}: 'extends' must be a filename string")
    parent = _resolve_with_extends((path.parent / parent_name).resolve(), _depth + 1)
    return _deep_merge(parent, data)


def _format_validation_error(exc: ValidationError, path: Path) -> str:
    lines = [f"configuration in {path} is invalid:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def load_config(path: str | Path | None = None) -> LoadedConfig:
    """Load, merge, and validate configuration.

    Resolution order: explicit ``path`` → ``$BTCBOT_CONFIG`` → ``config/research.yaml``.
    """
    candidate = path or os.getenv("BTCBOT_CONFIG") or "config/research.yaml"
    resolved = Path(candidate).resolve()
    merged = _resolve_with_extends(resolved)

    try:
        config = AppConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, resolved)) from exc

    # Hash the *validated, fully-defaulted* config so the recorded hash reflects
    # what actually ran, not just what the operator typed.
    canonical = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return LoadedConfig(
        config=config,
        config_hash=config_hash(canonical),
        source_path=resolved,
        raw=merged,
    )


def load_credentials(*, env_file: str | Path | None = ".env", required: bool = True) -> Credentials | None:
    """Read demo credentials from the environment.

    Credentials come only from the environment (optionally seeded from ``.env``).
    They are never read from YAML, never written to the database, and are
    registered with the log redaction filter the moment they are loaded.
    """
    if env_file:
        env_path = Path(env_file)
        if env_path.exists():
            load_dotenv(env_path, override=False)

    api_key = (os.getenv("OKX_DEMO_API_KEY") or "").strip()
    api_secret = (os.getenv("OKX_DEMO_API_SECRET") or "").strip()
    passphrase = (os.getenv("OKX_DEMO_PASSPHRASE") or "").strip()

    if not api_key or not api_secret or not passphrase:
        if required:
            raise CredentialsMissingError(
                "OKX demo credentials not found.\n"
                "  1. cp .env.example .env\n"
                "  2. Set OKX_DEMO_API_KEY, OKX_DEMO_API_SECRET and OKX_DEMO_PASSPHRASE in .env\n"
                "     (create the key inside OKX's Demo Trading area — see README §9)"
            )
        return None

    register_secret(api_key)
    register_secret(api_secret)
    register_secret(passphrase)
    return Credentials(api_key=api_key, api_secret=api_secret, passphrase=passphrase)


def optional_env(name: str) -> str | None:
    """Read an optional environment value (news/notification keys), redacting it in logs."""
    value = (os.getenv(name) or "").strip() or None
    if value:
        register_secret(value)
    return value
