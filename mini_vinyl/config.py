"""Loads config/secrets.env."""

import os
from pathlib import Path
from dataclasses import dataclass

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


@dataclass(frozen=True)
class TagEntry:
    uid: str
    type: str  # "youtube" | "spotify"
    id: str
    title: str = ""


def load_secrets(path: Path | None = None) -> None:
    """Loads config/secrets.env into os.environ (no-op if missing)."""
    path = path or CONFIG_DIR / "secrets.env"
    if path.exists():
        load_dotenv(path)


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value
