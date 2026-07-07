"""Loads config/tags.yaml and config/secrets.env."""

import os
from pathlib import Path
from dataclasses import dataclass

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


@dataclass(frozen=True)
class TagEntry:
    uid: str
    type: str  # "youtube" | "spotify"
    id: str
    title: str = ""


def load_tags(path: Path | None = None) -> dict[str, TagEntry]:
    path = path or CONFIG_DIR / "tags.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy config/tags.example.yaml to config/tags.yaml "
            "and fill in your tag UIDs."
        )

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    tags = {}
    for uid, entry in (raw.get("tags") or {}).items():
        uid_norm = uid.strip().upper()
        tags[uid_norm] = TagEntry(
            uid=uid_norm,
            type=entry["type"],
            id=entry["id"],
            title=entry.get("title", ""),
        )
    return tags


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
