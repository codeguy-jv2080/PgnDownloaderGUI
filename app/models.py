"""Validated inputs for the local download service."""

from __future__ import annotations

import re
import sys
import os
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


VENDOR_DIRECTORY = Path(__file__).resolve().parents[1] / "vendor"
if str(VENDOR_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIRECTORY))

from pgn_downloader.date_parser import parse_date  # noqa: E402


MODES: dict[str, tuple[str, ...]] = {
    "lichess": (
        "ultraBullet", "bullet", "blitz", "rapid", "classical", "correspondence",
        "chess960", "crazyhouse", "antichess", "atomic", "horde",
        "kingOfTheHill", "racingKings", "threeCheck",
    ),
    "chess.com": ("bullet", "blitz", "rapid", "correspondence"),
}
DEFAULT_MODES: dict[str, tuple[str, ...]] = {
    "lichess": ("blitz", "rapid", "classical", "correspondence"),
    "chess.com": ("blitz", "rapid", "correspondence"),
}

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{number}" for number in "123456789¹²³"),
    *(f"LPT{number}" for number in "123456789¹²³"),
}


def _absolute_directory(value: str) -> str:
    value = value.strip()
    if not value or not Path(value).is_absolute():
        raise ValueError("Choose an absolute output folder path.")
    if any(ord(character) < 32 for character in value):
        raise ValueError("The output folder contains invalid characters.")
    if value.startswith(("\\\\", "//")):
        raise ValueError("Choose a local output folder, not a network share.")
    resolved = Path(value).resolve()
    if str(resolved).startswith(("\\\\", "//")):
        raise ValueError("Choose a local output folder, not a network share.")
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(resolved.anchor)) == 4:
            raise ValueError("Choose a local output folder, not a network drive.")
    return value


def _parse_dates(since: str | None, until: str | None) -> tuple[datetime | None, datetime | None]:
    try:
        first = parse_date(since) if since else None
        last = parse_date(until, end=True) if until else None
    except (ValueError, IndexError, OverflowError, OSError) as error:
        raise ValueError(
            "Use a valid date (YYYY, YYYY-MM, YYYY-MM-DD) or a relative date such as 3m or 7d."
        ) from error
    if first is not None and last is not None and first > last:
        raise ValueError("The start date must be on or before the end date.")
    return first, last


class DownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: Literal["lichess", "chess.com"]
    username: str
    color: Literal["white", "black"] | None = None
    since: str | None = Field(default=None, max_length=64)
    until: str | None = Field(default=None, max_length=64)
    modes: list[str] | None = None
    output_dir: str
    filename: str | None = None

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
            raise ValueError("Use a username of 1–64 letters, numbers, underscores or hyphens.")
        return value

    @field_validator("since", "until", mode="before")
    @classmethod
    def normalize_optional_date(cls, value: object) -> object:
        return (value.strip() or None) if isinstance(value, str) else value

    @field_validator("output_dir")
    @classmethod
    def validate_directory(cls, value: str) -> str:
        return _absolute_directory(value)

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if len(value) > 255 or re.search(r'[<>:"/\\|?*\x00-\x1f]', value):
            raise ValueError("Use a filename without Windows path separators or reserved characters.")
        if value.endswith((".", " ")) or not value.lower().endswith(".pgn"):
            raise ValueError("The filename must end in .pgn.")
        if value.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            raise ValueError("That filename is reserved by Windows.")
        return value

    @model_validator(mode="after")
    def validate_filters(self) -> DownloadRequest:
        if self.modes is None:
            self.modes = list(DEFAULT_MODES[self.server])
        else:
            self.modes = list(dict.fromkeys(mode.strip() for mode in self.modes))
            if not self.modes or any(mode not in MODES[self.server] for mode in self.modes):
                raise ValueError("Select at least one supported game mode for the selected site.")
        _parse_dates(self.since, self.until)
        return self

    def parsed_dates(self) -> tuple[datetime | None, datetime | None]:
        return _parse_dates(self.since, self.until)

    @property
    def generated_filename(self) -> str:
        if self.filename:
            return self.filename
        color_suffix = f"-{self.color}" if self.color else ""
        return f"{self.server}_{self.username}{color_suffix}.pgn"


class OutputSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    output_dir: str

    @field_validator("output_dir")
    @classmethod
    def validate_directory(cls, value: str) -> str:
        return _absolute_directory(value)


class AppearanceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    theme: Literal["light", "dark"]
