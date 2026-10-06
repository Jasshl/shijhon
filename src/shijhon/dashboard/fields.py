"""Settings pages generated from the settings definitions (``config.py``).

Each field's type, limits and built-in default decide its control and its checks; the table
below only adds words (label, help, unit, group). A setting without an entry still appears,
in its section's last group (under "More settings"), with a label made from its key and the
field's description as its help: settings added elsewhere show up without new dashboard code.

Numbers are text fields: a comma or a dot is the decimal separator ("4,5" and "4.5"),
spaces are ignored, and the value and its default are written by one formatter (a dot, no
trailing zeros).
"""

from __future__ import annotations

import math
import re
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import annotated_types
from pydantic import BaseModel, SecretStr
from pydantic.fields import FieldInfo


class Invalid(ValueError):
    """A value the field does not accept; the message says what to enter."""


@dataclass(frozen=True)
class Meta:
    label: str
    help: str = ""
    group: str = ""
    unit: str | None = None  # None: from the key's suffix (_seconds: "s")
    spoken: str | None = None  # the unit for screen readers ("seconds")
    options: Mapping[str, str] = field(default_factory=dict)  # choice labels
    choices: str | None = None  # "sources": a choice of the add-ons by name
    short: bool = False  # a short code (a region)
    max_length: int | None = None  # a text's length at most
    pattern: str | None = None  # a text's extra check
    pattern_error: str = ""
    cross: str = ""  # the row's message when a rule across settings fails ({other} values)
    cross_notice: str = ""
    percent: bool = False  # a share from 0 to 1 shown as a whole percentage
    # Another setting shown in this row, after this one's control and the word ``joiner``
    # (two ends of a range: "from [any] to [lossless]"); it has no row of its own.
    pair: str = ""
    joiner: str = ""


@dataclass(frozen=True)
class Group:
    key: str
    title: str
    help: str = ""
    # Under the page's "More settings", collapsed: settings few people change. A section's
    # last group takes the settings the words table does not know, so it is one of these.
    more: bool = False


# The ends of a DASH quality range.
_QUALITIES = {
    "128": "128 kb/s",
    "192": "192 kb/s",
    "256": "256 kb/s",
    "320": "320 kb/s",
    "lossless": "Lossless",
}

# Each section's groups in page order: those most people change first, then the ones under
# "More settings". A group's key stays as it is (a catalog adapter names "connection",
# "albums" or "advanced" for its own settings).
GROUPS: dict[str, list[Group]] = {
    "delivery": [
        Group("sources", "Choosing an add-on"),
        Group(
            "dash",
            "DASH links",
            "Some add-ons link to a DASH stream rather than to a file. Shijhon joins its"
            " segments into one file without converting the audio; it needs ffmpeg.",
        ),
        Group("timing", "Timing", more=True),
        Group("fallbacks", "Fallbacks", more=True),
        Group("cooldown", "Add-ons that fail", more=True),
        Group("warm", "Opening the next songs", more=True),
        Group(
            "downloads",
            "Downloaded audio",
            "Songs fetched whole replace their silent placeholder for a while. How long, and"
            " how much is kept, is set on the Cleanup page.",
            more=True,
        ),
        Group("limits", "Limits per user", more=True),
        Group(
            "addons",
            "Limits per add-on",
            "What Shijhon sends one add-on, for everyone together; add-ons at one address"
            " share them. An add-on can have its own on the Add-ons page.",
            more=True,
        ),
        Group("joining", "Joining DASH streams", more=True),
        Group("advanced", "Connections to the add-ons", more=True),
    ],
    "fill": [
        Group("policy", "Fill policy"),
        Group("pass", "Library pass"),
        Group("albums", "Partly owned albums", more=True),
        Group("matching", "Matching", more=True),
        Group("advanced", "Library pass schedule", more=True),
    ],
    "cleanup": [Group("cleanup", "Cleanup settings")],
    "catalog": [
        Group("connection", "Connection"),
        Group("albums", "Albums and songs"),
        Group("covers", "Covers", more=True),
        Group("advanced", "Requests to the catalog", more=True),
    ],
}

_WRITE_ONLY = "Write-only: stored on the server and never shown."

# The words of each setting; a section's settings appear in this order.
META: dict[str, dict[str, Meta]] = {
    "delivery": {
        "routing": Meta(
            "Routing mode",
            "How the add-on for a song is picked. In order goes down the add-on list; ready"
            " first takes the first that has the song ready; primary first starts the primary.",
            "sources",
            options={
                "ordered": "In order",
                "ready_first": "Ready first",
                "primary_first": "Primary first",
            },
        ),
        "primary_source": Meta(
            "Primary add-on",
            "Tried first when the routing mode is primary first.",
            "sources",
            choices="sources",
        ),
        "reliable_source": Meta(
            "Preferred fallback",
            "Under ready first, used when no add-on has the song ready. Under primary first,"
            " the first fallback until recent plays show the fastest.",
            "sources",
            choices="sources",
        ),
        "budget_seconds": Meta(
            "Time to first audio",
            "How long a song may take to start, one fallback included; under primary first the"
            " primary may use all of it, and the fallbacks get their own. An add-on can have"
            " its own time on the Add-ons page.",
            "sources",
            cross="Must be at most the longest wait ({max_wait_seconds} s), or apps give up"
            " before an add-on had its chance.",
            cross_notice="is above the longest wait.",
        ),
        "dash": Meta(
            "Play DASH links",
            "Off: an add-on's DASH link is not played, and the next add-on is asked.",
            "dash",
        ),
        "dash_quality_from": Meta(
            "DASH quality: from",
            "Of the qualities a stream offers, the highest in this range is played. A song"
            " with none in it plays from the next add-on.",
            "dash",
            options=_QUALITIES | {"any": "Any"},
            pair="dash_quality_to",
            joiner="to",
            cross="Must be at most the other end ({dash_quality_to}).",
            cross_notice="is above the other end of the range.",
        ),
        "dash_quality_to": Meta("DASH quality: to", "", "dash", options=_QUALITIES),
        "primary_budget_seconds": Meta(
            "Primary's time to first audio",
            "Under primary first: how long the primary has to start the audio before an"
            " add-on with the song ready takes over.",
            "timing",
        ),
        "max_wait_seconds": Meta(
            "Longest wait",
            "An app's whole wait for the first audio. Keep it below the apps' own timeouts.",
            "timing",
            cross="Must be at least the time to first audio ({budget_seconds} s), or apps give"
            " up before an add-on had its chance.",
            cross_notice="is below the time to first audio.",
        ),
        "availability_timeout_seconds": Meta(
            "Ready check timeout",
            "How long the other add-ons have to say whether they have a song ready.",
            "timing",
        ),
        "reliable_lookup_after_seconds": Meta(
            "Fallback lookup after",
            "Under primary first, the likely fallback looks the song up, without playing it,"
            " once the primary has taken this long. 0: with every play.",
            "timing",
        ),
        "seek_timeout_seconds": Meta(
            "Seek timeout", "How long a jump within a playing song may take.", "timing"
        ),
        "ahead_window_seconds": Meta(
            "Fetch-ahead detection",
            "A song that starts this soon after another on the same app, and isn't reported"
            " as playing, counts as fetched ahead. 0: off.",
            "timing",
        ),
        "prepare_when_not_ready": Meta(
            "Prepare for next time",
            "Under ready first: when no add-on has a song ready, one is asked to prepare it"
            " for the next play.",
            "fallbacks",
        ),
        "max_attempts": Meta(
            "Attempts per play",
            "How many add-ons get time to start a song (under primary first, the fallbacks"
            " after the primary). One that lacks the song, or fails at once, doesn't count.",
            "fallbacks",
            unit="attempts",
        ),
        "length_tolerance_seconds": Meta(
            "Length difference allowed",
            "Audio whose length differs from the catalog's by more than this, or the share"
            " below if larger, is another recording: the next add-on plays. Both 0: no check.",
            "fallbacks",
        ),
        "length_tolerance_percent": Meta(
            "Length difference as a share",
            "The same, as a share of the song's length.",
            "fallbacks",
            unit="%",
            spoken="percent",
        ),
        "cooldown_seconds": Meta(
            "Cooldown",
            "How long an add-on that keeps failing is passed over, so apps don't wait for it.",
            "cooldown",
        ),
        "cooldown_errors": Meta(
            "Errors before cooldown",
            "An add-on that answers with this many errors in a row cools down, and again after"
            " each further error until it plays. 0: off.",
            "cooldown",
            unit="errors",
        ),
        "primary_cooldown_timeouts": Meta(
            "Timeouts before cooldown",
            "The primary cools down after this many timeouts without a delivery in between.",
            "cooldown",
            unit="timeouts",
        ),
        "primary_cooldown_switches": Meta(
            "Switches before cooldown",
            "Or after this many switches away from it to an add-on that had the song ready.",
            "cooldown",
            unit="switches",
        ),
        "primary_miss_hours": Meta(
            "Songs the primary lacks",
            "A song the primary did not have is not asked for there again for this long. 0: off.",
            "cooldown",
        ),
        "primary_release_miss_minutes": Meta(
            "Albums the primary lacks",
            "Once the primary lacked a song of an album, its other songs are also looked up"
            " at the next add-on at once for this long. 0: off.",
            "cooldown",
        ),
        "retry_skip_seconds": Meta(
            "Retries skip failed add-ons",
            "A retry of a song that failed skips the add-ons that failed for it within this"
            " long. 0: off.",
            "cooldown",
        ),
        "warm_ahead_depth": Meta(
            "Next songs opened",
            "How many of the next songs are opened when one starts playing, so they start"
            " quickly. 0: off.",
            "warm",
            unit="songs",
        ),
        "warm_ahead_jobs": Meta(
            "Plays at once",
            "How many plays have their next songs opened at the same time, for all listeners"
            " together. More wait their turn, after the songs being played.",
            "warm",
            unit="plays",
        ),
        "warm_ahead_delay_seconds": Meta(
            "Wait before opening",
            "How long after a song's first audio the next ones are opened.",
            "warm",
        ),
        "warm_ahead_budget_seconds": Meta(
            "Time to open one", "How long opening one of the next songs may take.", "warm"
        ),
        "prefetch_memory_seconds": Meta(
            "Apps that fetch ahead",
            "An app that fetches the next songs itself gets none opened for it for this long.",
            "warm",
        ),
        "download_timeout_seconds": Meta(
            "Download timeout", "How long fetching a whole song may take.", "downloads"
        ),
        "delivered_days": Meta(
            "Kept for",
            "A fetched song goes back to its silent placeholder after this many days unused."
            " 0: no age limit.",
            "downloads",
        ),
        "delivered_gb": Meta(
            "At most",
            "Past this size in all, the least recently used go back first. 0: no size limit.",
            "downloads",
            unit="GB",
            spoken="gigabytes",
        ),
        "user_routings": Meta(
            "Songs looked up at once",
            "Songs one user's apps look up at the add-ons at the same time; more wait their"
            " turn. 0: no limit.",
            "limits",
            unit="songs",
        ),
        "user_downloads": Meta(
            "Downloads at once",
            "Whole songs fetched for one user at the same time: downloads, and plays that need"
            " converting. Ordinary streams don't count. 0: no limit.",
            "limits",
            unit="songs",
        ),
        "user_downloads_per_hour": Meta(
            "Downloads an hour",
            "Whole-song fetches one user may start in an hour; more wait their turn, never the"
            " song being played. A share link counts as a user. 0: no limit.",
            "limits",
            unit="songs",
        ),
        "user_download_burst": Meta(
            "Downloads in a row",
            "How many start without a wait after a quiet while. 0: the whole hour's at once.",
            "limits",
            unit="songs",
        ),
        "addon_requests_per_second": Meta(
            "Requests a second",
            "Lookups, links and ready checks sent to one add-on; more wait their turn."
            " 0: no limit.",
            "addons",
            unit="/s",
            spoken="requests a second",
        ),
        "addon_request_burst": Meta(
            "Requests at once",
            "How many may go at once after a quiet moment, before the rate applies.",
            "addons",
            unit="requests",
        ),
        "addon_audio_openings": Meta(
            "Songs opened at once",
            "Songs whose audio one add-on is asked for at the same time; the song being played"
            " never waits. 0: no limit.",
            "addons",
            unit="songs",
        ),
        "dash_start": Meta(
            "DASH songs start",
            "At once: a song plays from its first segments. When joined: once all its segments"
            " are one file.",
            "joining",
            options={"at_once": "At once", "complete": "When joined"},
        ),
        "dash_segments_at_once": Meta(
            "Segments at once",
            "How many of a song's segments are fetched at the same time.",
            "joining",
            unit="segments",
        ),
        "dash_joins_at_once": Meta(
            "Songs joined at once",
            "For all listeners together; the songs being played never wait.",
            "joining",
            unit="songs",
        ),
        "dash_cache_mb": Meta(
            "Joined files kept",
            "Kept for later plays and seeks; past this size the least recently used go first."
            " 0: only the newest.",
            "joining",
        ),
        "request_timeout_seconds": Meta(
            "Add-on request timeout", "How long one request to an add-on may take.", "advanced"
        ),
        "pin_ttl_seconds": Meta(
            "Link reuse",
            "How long a song keeps its add-on and link, for seeks and repeats.",
            "advanced",
        ),
    },
    "fill": {
        "auto_min_songs": Meta(
            "At least this many songs",
            "An album is filled automatically when you own at least this many of its songs.",
            "policy",
            unit="songs",
        ),
        "auto_min_share": Meta(
            "At least this share of the album",
            "Or at least this share of its tracks.",
            "policy",
            unit="%",
            spoken="percent",
            percent=True,
        ),
        "library_pass": Meta(
            "Library pass",
            "Matches every album against the catalog. A dry run fills nothing automatically"
            " and lists what it would fill; on fills the albums that meet the policy.",
            "pass",
            options={"off": "Off", "dry_run": "Dry run", "on": "On"},
        ),
        "enabled": Meta(
            "Fill partly owned albums",
            "Off: albums are never matched or filled, and the Library page lists nothing.",
            "albums",
        ),
        "complete_lists": Meta(
            "Complete albums in lists",
            "A partly owned album shown complete has the complete song count and length in"
            " lists and searches too. Off: the owned songs' count until it is filled.",
            "albums",
        ),
        "open_budget_seconds": Meta(
            "Wait for a match on first view",
            "How long an album seen for the first time waits for its match; after it, it"
            " shows your songs only.",
            "matching",
        ),
        "sync_albums": Meta(
            "Albums before background matching",
            "An app that views this many never-matched albums within the time below is"
            " syncing: they are matched in the background.",
            "matching",
            unit="albums",
        ),
        "sync_seconds": Meta("Counted within", "The time for the count above.", "matching"),
        "retry_hours": Meta(
            "Retry after a failure",
            "A failed match is tried again after this long, then twice as long.",
            "matching",
        ),
        "background_pause_seconds": Meta(
            "Pause between background matches",
            "Between albums matched in the background, and after each fill the pass makes.",
            "matching",
        ),
        "pass_start_seconds": Meta(
            "First pass after", "How long after Shijhon starts the library pass begins.", "advanced"
        ),
        "pass_interval_hours": Meta(
            "Pass repeats every",
            "How often the pass looks again for new albums and failures.",
            "advanced",
        ),
        "pass_requests_per_second": Meta(
            "Requests per second",
            "Catalog requests the library pass makes at most.",
            "advanced",
            unit="per s",
            spoken="requests per second",
        ),
    },
    "search": {
        "budget_seconds": Meta(
            "Wait for the catalog",
            "The longest a search or an artist page in your library waits for the catalog;"
            " the answer goes out as soon as it has replied. After it, your library's results"
            " show alone.",
            "settings",
        ),
    },
    "cleanup": {
        "mode": Meta(
            "Cleanup",
            "Once a day, takes whole releases nobody has used out of the library again; a dry"
            " run only lists them. A release taken out comes back when one of its songs is used.",
            "cleanup",
            options={"off": "Off", "dry_run": "Dry run", "on": "On"},
        ),
        "unused_days": Meta(
            "Taken out after",
            "A release nobody has used is taken out this many days after it was added.",
            "cleanup",
        ),
        "catalog_albums": Meta(
            "Take out catalog albums",
            "Albums added to the library when one of their songs was used.",
            "cleanup",
        ),
        "fills": Meta(
            "Take out songs added to your albums",
            "The songs that complete a partly owned album. Taken out, the album is shown"
            " complete again and filled on its next use.",
            "cleanup",
        ),
    },
    "catalog": {
        # Its choices are the installed catalog adapters; an adapter's own settings follow
        # it, with the words the adapter declares (``sections.fields_of``).
        "kind": Meta(
            "Catalog",
            "The service for searches, artist pages and album track lists: one of the"
            ' installed adapters, or "An add-on\'s catalog" from one of your add-ons.',
            "connection",
        ),
        "twins": Meta(
            "Clean and explicit versions",
            "Which version of an album or song is shown when the catalog has both.",
            "albums",
            options={"explicit": "Explicit", "clean": "Clean", "both": "Both"},
        ),
        "artwork_size": Meta(
            "Cover size", "The size of covers written for catalog albums.", "covers", unit="px"
        ),
        "cover_sizes": Meta(
            "Cover sizes",
            "Covers are fetched at the next of these sizes up from the one an app asks for."
            " Empty: the exact size.",
            "covers",
            unit="px",
        ),
        "artwork_cache_mb": Meta(
            "Cover cache",
            "Resized covers kept on disk; past this size the oldest go first. 0: none kept.",
            "covers",
        ),
        "artwork_cache_days": Meta(
            "Covers kept for", "How long a resized cover is kept on disk.", "covers"
        ),
        "prefetch_covers": Meta(
            "Covers fetched ahead",
            "Covers of the first catalog items on an artist page or in search results,"
            " fetched while the app loads the page. 0: none.",
            "covers",
        ),
        "prefetch_parallel": Meta(
            "Covers fetched at once", "Covers fetched ahead at the same time.", "covers"
        ),
        "prefetch_burst_pages": Meta(
            "Pages before the pause",
            "An app shown this many pages with catalog items within the time below gets no"
            " covers fetched ahead meanwhile.",
            "covers",
        ),
        "prefetch_burst_seconds": Meta(
            "Pages counted within", "The time for the pages above.", "covers"
        ),
        "cache_seconds": Meta(
            "Answers reused for", "How long a catalog answer is kept.", "advanced"
        ),
        "timeout_seconds": Meta(
            "Request timeout", "How long one request to the catalog may take.", "advanced"
        ),
    },
}

_SUFFIXES = (
    ("_per_second", "per s", "per second"),
    ("_mb", "MB", "megabytes"),
    ("_seconds", "s", "seconds"),
    ("_minutes", "min", "minutes"),
    ("_hours", "h", "hours"),
    ("_days", "days", "days"),
)


def spoken_unit(label: str, spoken: str) -> str:
    """The unit a screen reader hears after a setting's label: none when the label already
    has its words ("Requests a second" needs no "(requests a second)", "Songs opened at
    once" no "(songs)")."""
    said = set(re.findall(r"\w+", label.casefold()))
    if all(word in said for word in re.findall(r"\w+", spoken.casefold())):
        return ""
    return spoken


def format_number(value: float | int) -> str:
    """A dot as the decimal separator, no trailing zeros and no rounding: 4.5, 9, 0.8,
    1800, 0.1234567 (the shortest text that reads back as the same number)."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    text = format(Decimal(repr(float(value))), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def parse_number(raw: str, *, whole: bool) -> float | int:
    """A number typed with a comma or a dot as the decimal separator."""
    text = "".join(raw.split())  # spaces, also between digit groups
    if text.count(",") == 1 and "." not in text:
        text = text.replace(",", ".")
    if not re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)", text):
        raise Invalid("Enter a whole number, e.g. 3." if whole else "Enter a number, e.g. 4.5.")
    number = float(text)
    if not math.isfinite(number):
        raise Invalid("Enter a number, e.g. 4.5.")
    if whole:
        if not number.is_integer():
            raise Invalid("Enter a whole number, e.g. 3.")
        return int(number)
    return number


@dataclass(frozen=True)
class SettingField:
    section: str
    key: str
    kind: str  # decimal | whole | switch | select | text | secret | path | mapping
    meta: Meta
    builtin: Any
    optional: bool = False
    low: float | None = None
    low_open: bool = False
    high: float | None = None
    high_open: bool = False
    values: tuple[str, ...] = ()  # a select's values (a Literal's)
    scale: float = 1  # shown as the value times this (a share as a percentage: 100)

    @property
    def id(self) -> str:
        return f"{self.section}-{self.key}".replace("_", "-")

    @property
    def unit(self) -> str:
        if self.meta.unit is not None:
            return self.meta.unit
        for suffix, unit, _ in _SUFFIXES:
            if self.key.endswith(suffix):
                return unit
        return ""

    @property
    def spoken(self) -> str:
        if self.meta.spoken is not None:
            return self.meta.spoken
        for suffix, unit, spoken in _SUFFIXES:
            if self.key.endswith(suffix) and self.unit == unit:
                return spoken
        return ""

    @property
    def editable(self) -> bool:
        return self.kind != "mapping"

    def options(self) -> list[tuple[str, str]]:
        return [
            (value, self.meta.options.get(value, value.replace("_", " ").capitalize()))
            for value in self.values
        ]

    def rule(self) -> str:
        """The accepted range in words: "from 1 to 60", "above 0", "at least 2"."""
        low = None if self.low is None else format_number(self.low)
        high = None if self.high is None else format_number(self.high)
        if low is not None and high is not None:
            if not self.low_open and not self.high_open:
                return f"from {low} to {high}"
            return (
                f"{'above' if self.low_open else 'from'} {low},"
                f" {'below' if self.high_open else 'up to'} {high}"
            )
        if low is not None:
            return f"above {low}" if self.low_open else f"of at least {low}"
        if high is not None:
            return f"below {high}" if self.high_open else f"of at most {high}"
        return ""

    def _range_message(self) -> str:
        if self.scale == 100:
            return f"Enter a percentage {self.rule()}, e.g. 25."
        noun = "a whole number" if self.kind == "whole" else "a number"
        rule = self.rule()
        return f"Enter {noun} {rule}." if rule else f"Enter {noun}."

    def notice(self, message: str) -> str:
        """The page notice's rule for this field (after its label)."""
        if message.startswith("Enter "):
            return "must be " + message.removeprefix("Enter ").rstrip(".") + "."
        return message[0].lower() + message[1:] if message else message

    def parse(self, raw: str | None) -> Any:
        """The value of the submitted text; raises :class:`Invalid`. Secrets are handled by
        the caller (an empty field keeps the current one)."""
        text = (raw or "").strip()
        if self.kind in ("decimal", "whole"):
            if not text:
                if self.optional:
                    return None
                raise Invalid(self._range_message())
            try:
                number = parse_number(text, whole=self.kind == "whole")
            except Invalid:
                if self.scale != 1:
                    raise Invalid(self._range_message()) from None
                raise
            if self.low is not None and (
                number <= self.low if self.low_open else number < self.low
            ):
                raise Invalid(self._range_message())
            if self.high is not None and (
                number >= self.high if self.high_open else number > self.high
            ):
                raise Invalid(self._range_message())
            return number / self.scale if self.scale != 1 else number
        if self.kind == "switch":
            return text == "true"
        if self.kind == "numbers":  # "100, 150, 200" (empty: none)
            parts = [part for part in re.split(r"[\s,;]+", text) if part]
            if not all(re.fullmatch(r"[0-9]+", part) and int(part) > 0 for part in parts):
                raise Invalid("Enter whole numbers above 0, separated by commas.")
            return sorted({int(part) for part in parts})  # as the setting keeps them
        if self.kind == "select":
            if text not in self.values:
                raise Invalid("Choose one of the listed values.")
            return text
        if self.kind == "path":
            return Path(text) if text else None
        if self.meta.pattern and not re.fullmatch(self.meta.pattern, text):
            raise Invalid(self.meta.pattern_error or "Enter a valid value.")
        return text

    def show(self, value: Any) -> str:
        """The field's text for a value (never a secret's)."""
        if value is None or self.kind in ("secret", "mapping"):
            return ""
        if self.kind in ("decimal", "whole"):
            return format_number(self._shown(value))
        if self.kind == "numbers":
            return ", ".join(str(v) for v in value)
        return str(value)

    def _shown(self, value: float) -> float:
        return round(value * self.scale, 9) if self.scale != 1 else value

    def describe(self, value: Any) -> str:
        """A value in words, for "Default …" lines."""
        if self.kind == "secret":
            return "set" if value is not None else "not set"
        if self.kind == "mapping":
            return f"{len(value)} set" if value else "none"
        if self.kind == "switch":
            return "On" if value else "Off"
        if self.kind == "numbers":
            return f"{', '.join(str(v) for v in value)} {self.unit}".strip() if value else "none"
        if self.kind == "select":
            return dict(self.options()).get(str(value), str(value))
        if value is None or value == "":
            return "None"
        if self.kind in ("decimal", "whole"):
            unit = self.unit
            return f"{format_number(self._shown(value))} {unit}".strip()
        return str(value)


def _label(key: str) -> str:
    for suffix, _, _ in _SUFFIXES:
        if key.endswith(suffix) and key != suffix.lstrip("_"):
            key = key.removesuffix(suffix)
            break
    return key.replace("_", " ").capitalize()


def _bounds(info: FieldInfo) -> dict[str, Any]:
    bounds: dict[str, Any] = {}
    for item in info.metadata:
        if isinstance(item, annotated_types.Ge):
            bounds.update(low=float(item.ge), low_open=False)  # type: ignore[arg-type]
        elif isinstance(item, annotated_types.Gt):
            bounds.update(low=float(item.gt), low_open=True)  # type: ignore[arg-type]
        elif isinstance(item, annotated_types.Le):
            bounds.update(high=float(item.le), high_open=False)  # type: ignore[arg-type]
        elif isinstance(item, annotated_types.Lt):
            bounds.update(high=float(item.lt), high_open=True)  # type: ignore[arg-type]
    return bounds


def _kind(annotation: Any) -> tuple[str, bool, tuple[str, ...]]:
    """(kind, optional, select values) of a field's type."""
    optional = False
    args = typing.get_args(annotation)
    if typing.get_origin(annotation) in (typing.Union, types.UnionType) and type(None) in args:
        optional = True
        rest = [arg for arg in args if arg is not type(None)]
        annotation = rest[0] if len(rest) == 1 else annotation
    origin = typing.get_origin(annotation)
    if origin is Literal:
        return "select", optional, tuple(str(value) for value in typing.get_args(annotation))
    if origin in (dict, Mapping) or annotation is dict:
        return "mapping", optional, ()
    if origin in (list, tuple) and typing.get_args(annotation)[:1] == (int,):
        return "numbers", optional, ()  # "100, 150, 200"
    if annotation is bool:
        return "switch", optional, ()
    if annotation is int:
        return "whole", optional, ()
    if annotation is float:
        return "decimal", optional, ()
    if annotation is SecretStr:
        return "secret", optional, ()
    if annotation is Path:
        return "path", optional, ()
    return "text", optional, ()


def section_fields(
    section: str,
    model: type[BaseModel],
    *,
    extra: Mapping[str, Meta] | None = None,
    after: str | None = None,
) -> list[SettingField]:
    """Every field of a configuration section, in the words table's order first. ``extra``:
    words for settings the table does not know (a catalog adapter's own), placed after
    the setting ``after``."""
    words = {**META.get(section, {}), **(extra or {})}
    groups = GROUPS.get(section) or [Group("settings", "Settings")]
    fallback = groups[-1].key
    found = []
    for key, info in model.model_fields.items():
        kind, optional, values = _kind(info.annotation)
        meta = words.get(key) or Meta(_label(key), info.description or "", fallback)
        if not meta.group:
            meta = Meta(**{**meta.__dict__, "group": fallback})
        bounds = _bounds(info)
        scale = 1.0
        if meta.percent:  # a share (0 to 1) typed as a percentage (12.5 accepted too)
            kind, scale = "decimal", 100.0
            bounds = {k: v * scale if k in ("low", "high") else v for k, v in bounds.items()}
        found.append(
            SettingField(
                section,
                key,
                kind,
                meta,
                info.get_default(call_default_factory=True),
                optional,
                values=values,
                scale=scale,
                **bounds,
            )
        )
    order = list(META.get(section, {}))
    if extra:
        at = order.index(after) + 1 if after in order else len(order)
        order[at:at] = [key for key in extra if key not in order]
    found.sort(key=lambda f: order.index(f.key) if f.key in order else len(order))
    return found


def grouped(section: str, fields: list[SettingField]) -> list[tuple[Group, list[SettingField]]]:
    groups = GROUPS.get(section) or [Group("settings", "Settings")]
    keys = {group.key for group in groups}
    result = []
    for group in groups:
        members = [
            f
            for f in fields
            if f.meta.group == group.key or (group is groups[-1] and f.meta.group not in keys)
        ]
        if members:
            result.append((group, members))
    return result
