"""Settings from environment variables, with an optional `.env` file (gitignored).

Real environment variables win over `.env`. Secrets are never printed: the
Settings repr masks the API key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


def read_env_file(path: str | Path) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE lines, # comments, optional quotes. Missing file -> {}."""
    path = Path(path)
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


@dataclass(frozen=True)
class Settings:
    llm_provider: str = "groq"
    llm_model: str = ""
    groq_api_key: str = field(default="", repr=False)
    anthropic_api_key: str = field(default="", repr=False)

    def __repr__(self) -> str:
        return (
            f"Settings(llm_provider={self.llm_provider!r}, llm_model={self.llm_model!r}, "
            f"groq_api_key={'set' if self.groq_api_key else 'missing'}, "
            f"anthropic_api_key={'set' if self.anthropic_api_key else 'missing'})"
        )


def load_settings(env_file: str | Path = ".env", environ: dict[str, str] | None = None) -> Settings:
    merged = {**read_env_file(env_file), **(os.environ if environ is None else environ)}
    return Settings(
        llm_provider=(merged.get("LLM_PROVIDER") or "groq").strip().lower(),
        llm_model=(merged.get("LLM_MODEL") or "").strip(),
        groq_api_key=(merged.get("GROQ_API_KEY") or "").strip(),
        anthropic_api_key=(merged.get("ANTHROPIC_API_KEY") or "").strip(),
    )
