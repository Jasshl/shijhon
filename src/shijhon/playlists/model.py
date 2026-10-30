"""Saved definitions and the bounded data exchanged with curators."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from shijhon.matching.normalize import (
    fold,
    recording_edition_marked,
    recording_key,
    version_marker,
)

# Bidirectional embeddings, overrides and isolates can make shown text read otherwise.
_BIDI_CONTROLS = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))


def is_text(value: str) -> bool:
    """Text as playlists and exports hold it: spaces of other widths (no-break, narrow,
    ideographic) and invisible marks (zero-width, left-to-right) are text; controls,
    line breaks and bidirectional overrides are not."""
    return all(
        (c.isprintable() or unicodedata.category(c) in ("Zs", "Cf")) and c not in _BIDI_CONTROLS
        for c in value
    )


# A Navidrome ID as callers name playlists and songs: plain characters only, so it can
# appear in a message.
NativeId = Annotated[
    str, StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
]
REFERENCE_PLAYLISTS = 3
REFERENCE_SONGS = 50
LIBRARY_LOST = "The agent connection no longer grants library access. Generate again."


def distinct(values: list[str]) -> list[str]:
    if len(set(values)) != len(values):
        raise ValueError("references repeat an ID")
    return values


class PlaylistError(Exception):
    """Only fixed, safe messages reach the dashboard; never remote error bodies."""


class UpstreamUnavailable(PlaylistError):
    """Native authorization could not be checked due to an upstream failure."""


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)


class Connection(Model):
    user_id: str
    username: str
    token: SecretStr
    salt: SecretStr
    history_user: str = Field(default="", max_length=100)
    history_key: SecretStr = SecretStr("")
    history_source: Literal["manual", "navidrome"] = "manual"
    backend: Literal["none", "api", "codex", "claude"] = "none"
    endpoint: str = Field(default="", max_length=2048)
    model: str = Field(default="", max_length=100)
    api_key: SecretStr = SecretStr("")
    use_shared_ai: bool = False

    @model_validator(mode="after")
    def runner_model(self) -> Connection:
        if self.endpoint:
            url = urlsplit(self.endpoint)
            if (
                url.scheme not in ("https", "http")
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("API URL must not contain credentials or query parameters")
        if (
            self.backend in ("codex", "claude")
            and self.model
            and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,99}", self.model)
        ):
            raise ValueError("invalid runner model")
        return self

    def same_settings(self, other: Connection | None) -> bool:
        """Fresh salted native credentials do not change the owner's playlist choices.

        Navidrome accepts earlier token/salt pairs after login, so in-flight work can
        keep its pair. Every native call still verifies credentials and current grants.
        """
        return other is not None and self.model_dump(exclude={"token", "salt"}) == other.model_dump(
            exclude={"token", "salt"}
        )

    def stored(self) -> str:
        data = self.model_dump(mode="json")
        for key in ("token", "salt", "history_key", "api_key"):
            data[key] = getattr(self, key).get_secret_value()
        return json.dumps(data)

    def require_history(self) -> None:
        if not self.history_user or not self.history_key.get_secret_value():
            raise PlaylistError("Connect a Last.fm username and API key to use listening history.")


class AIPreferences(Model):
    """Personal choices, without host connection or execution authority."""

    model: str = Field(default="", max_length=100)

    @field_validator("model")
    @classmethod
    def model_name(cls, value: str) -> str:
        if value != value.strip() or any(not c.isprintable() for c in value):
            raise ValueError("invalid model name")
        return value


class AIModels(Model):
    allowed_models: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("allowed_models")
    @classmethod
    def model_names(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or len(value) > 100:
                raise ValueError("invalid model name")
            AIPreferences.model_name(value)
        return list(dict.fromkeys(values))


class Suggestion(Model):
    """Untrusted music names, never executable instructions or native/catalog IDs."""

    artist: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=300)
    album: str = Field(default="", max_length=300)
    version: str = Field(default="", max_length=100)

    @field_validator("artist", "title", "album", "version")
    @classmethod
    def printable(cls, value: str) -> str:
        if not is_text(value):
            raise ValueError("music names must be printable")
        return value.strip()

    @model_validator(mode="after")
    def present(self) -> Suggestion:
        if not self.artist or not self.title:
            raise ValueError("artist and title are required")
        return self

    def wanted(self) -> dict[str, Any]:
        title = self.title
        suffixes = (f"({self.version})", f"[{self.version}]", f" - {self.version}")
        if self.version and not any(title.casefold().endswith(s.casefold()) for s in suffixes):
            title += f" ({self.version})"
        return {
            "artist": self.artist,
            "title": title,
            "album": self.album,
            "required_title": title if self.version or recording_edition_marked(title) else "",
            "required_album": self.album,
            "score": 0,
            "reason": "Requested recording",
        }


class Definition(Model):
    name: str = Field(default="Discovery mix", min_length=1, max_length=100)
    auto_name: bool = False
    count: int = Field(default=30, ge=1, le=1_000_000)
    unfamiliar_percent: int = Field(default=70, ge=0, le=100)
    max_per_artist: int = Field(default=2, ge=1, le=10)
    exclude_days: int = Field(default=14, ge=0, le=365)
    repeat_days: int = Field(default=28, ge=0, le=365)
    cadence: Literal["manual", "daily", "weekly"] = "weekly"
    brief: str = Field(default="", max_length=2000)
    ai: bool = False
    review_before_publishing: bool = False
    mode: Literal["discovery", "prompt", "tracks", "import"] = "discovery"
    use_history: bool = False
    use_library: bool = False
    save_instructions: bool = False
    tracks: list[Suggestion] = Field(default_factory=list, max_length=100)
    # Described playlists only: read again at every generation, so refreshes follow them.
    reference_playlists: list[NativeId] = Field(
        default_factory=list, max_length=REFERENCE_PLAYLISTS
    )
    reference_songs: list[NativeId] = Field(default_factory=list, max_length=REFERENCE_SONGS)
    exclude_reference: bool = True

    @field_validator("name")
    @classmethod
    def name_present(cls, value: str) -> str:
        if not value.strip() or not is_text(value):
            raise ValueError("a printable name is required")
        return value

    @field_validator("reference_playlists", "reference_songs")
    @classmethod
    def distinct_references(cls, values: list[str]) -> list[str]:
        return distinct(values)

    @model_validator(mode="after")
    def brief_needs_ai(self) -> Definition:
        if self.mode != "import" and self.count > 100:
            raise ValueError("curated playlists have at most 100 tracks")
        if self.mode == "import" and (
            self.ai
            or self.interval
            or self.use_history
            or self.use_library
            or self.tracks
            or self.auto_name
            or self.save_instructions
            or not self.review_before_publishing
        ):
            raise ValueError("imports are one-off reviewed selections without AI or context")
        if self.auto_name and self.mode != "prompt":
            raise ValueError("automatic naming needs prompt curation")
        if self.brief.strip() and not self.ai:
            raise ValueError("a brief needs AI curation")
        if self.mode == "prompt" and (not self.brief.strip() or not self.ai):
            raise ValueError("a prompt needs instructions and saved AI")
        if self.mode == "tracks" and (
            len(self.tracks) != self.count
            or self.ai
            or self.interval
            or self.use_history
            or self.use_library
        ):
            raise ValueError("explicit tracks are one-off, with no model or context")
        if self.mode != "discovery" and self.interval and not self.save_instructions:
            raise ValueError("refreshes require explicit saved instructions")
        if (self.reference_playlists or self.reference_songs) and self.mode != "prompt":
            raise ValueError("references need a described playlist")
        return self

    @property
    def references(self) -> bool:
        return bool(self.reference_playlists or self.reference_songs)

    @property
    def unfamiliar(self) -> int:
        return math.ceil(self.count * self.unfamiliar_percent / 100)

    @property
    def interval(self) -> int:
        return {"manual": 0, "daily": 86400, "weekly": 7 * 86400}[self.cadence]


class Track(Model):
    id: str
    artist: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=300)
    album: str = Field(default="", max_length=300)
    duration: int = Field(default=0, ge=0)
    isrc: str = ""
    explicit: bool = False
    clean: bool = False
    release: str = ""
    # Explicit caller constraints survive general same-recording normalization.
    required_title: str = Field(default="", max_length=500)
    required_album: str = Field(default="", max_length=300)
    unfamiliar: bool = False
    score: float = 0
    reason: str = Field(default="", max_length=500)
    # As the library or the catalog gives them (0 and none: unknown); hints for curation.
    year: int = Field(default=0, ge=0, le=9999)
    genres: list[str] = Field(default_factory=list, max_length=5)

    @property
    def key(self) -> str:
        return music_key(self.artist, self.title)


def music_key(artist: str, title: str) -> str:
    return f"{fold(artist)}:{recording_key(title)}:{version_marker(title) or ''}"


def fingerprint(data: dict[str, Any]) -> str:
    """Relevant native playlist state, including user edits to its metadata."""
    value = {k: data.get(k, "") for k in ("id", "name", "owner", "comment", "changed")}
    value["public"] = bool(data.get("public", False))
    value["entries"] = [s["id"] for s in data.get("entry", [])]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
