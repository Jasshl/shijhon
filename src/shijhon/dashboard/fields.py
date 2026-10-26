"""Settings pages generated from the settings definitions (``config.py``).

Each field's type, limits and built-in default decide its control and its checks; the table
below only adds words (label, help, unit, group). A setting without an entry still appears,
in its section's last group (under "Advanced settings"), with a label made from its key and
the field's description as its help: settings added elsewhere show up without new dashboard
code.

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
    # Under the page's "Advanced settings", collapsed: settings few people change. A section's
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
# "Advanced settings". A group's key stays as it is (a catalog adapter names "connection",
# "albums" or "advanced" for its own settings).
GROUPS: dict[str, list[Group]] = {
    "delivery": [
        Group(
            "sources",
            "Choosing an add-on",
            "When an app plays a song you don't own, Shijhon asks your add-ons for its audio.",
        ),
        Group(
            "bulk",
            "Downloads and playlist preparation",
            "Whole songs fetched into the library, for app downloads and for playlists"
            " prepared on the Downloads page. These limits are for all users together.",
        ),
        Group(
            "dash",
            "DASH links",
            "Some add-ons link to a DASH stream instead of a file. Shijhon joins its"
            " segments into one file without converting the audio (this needs ffmpeg).",
        ),
        Group("timing", "Timing", more=True),
        Group(
            "fallbacks",
            "Fallbacks",
            "When an add-on can't play a song, or sends another recording, the next one is asked.",
            more=True,
        ),
        Group(
            "cooldown",
            "Add-ons that fail",
            "An add-on that keeps failing cools down: it is passed over for a while, so apps"
            " don't wait for it. Songs an add-on lacked, or just failed to play, can also skip"
            " it for a while.",
            more=True,
        ),
        Group(
            "warm",
            "Opening the next songs",
            "When a song starts playing, Shijhon opens the next songs in the app's queue (or"
            " the album) at the add-ons, so they start quickly.",
            more=True,
        ),
        Group(
            "downloads",
            "Saved audio",
            "Songs fetched whole, and streamed songs when they are kept, replace their silent"
            " placeholder for a while. Whether streams are kept, how long and how much is set"
            " on the Cleanup page.",
            more=True,
        ),
        Group(
            "limits",
            "Limits per user",
            "Keep one user's apps from tying up the add-ons for everyone. Over a limit,"
            " requests wait their turn; the song being played never waits.",
            more=True,
        ),
        Group("bulk_more", "New audio preparation", more=True),
        Group(
            "addons",
            "Limits per add-on",
            "Limits on what Shijhon sends one add-on, for all users together. Add-ons at the"
            " same address share them. An add-on can have its own limits on the Add-ons page.",
            more=True,
        ),
        Group("joining", "Joining DASH streams", more=True),
        Group("advanced", "Connections to the add-ons", more=True),
    ],
    "fill": [
        Group("policy", "Fill policy"),
        Group("pass", "Library pass"),
        Group("albums", "Partly owned albums", more=True),
        Group(
            "matching",
            "Matching",
            "Albums are matched with a catalog release when an app opens them, and in the"
            " background.",
            more=True,
        ),
        Group("advanced", "Library pass schedule", more=True),
    ],
    "cleanup": [Group("cleanup", "Cleanup settings")],
    "audiomuse": [
        Group("connection", "Connection"),
        Group("advanced", "Requests and reading the analysis", more=True),
    ],
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
            "In order tries the add-ons from the top of the list. Ready first picks the first"
            " one that has the song ready. Primary first starts with the primary add-on.",
            "sources",
            options={
                "ordered": "In order",
                "ready_first": "Ready first",
                "primary_first": "Primary first",
            },
        ),
        "primary_source": Meta(
            "Primary add-on",
            "Asked first when the routing mode is Primary first.",
            "sources",
            choices="sources",
        ),
        "reliable_source": Meta(
            "Preferred fallback",
            "Ready first: used when no add-on has the song ready. Primary first: the first"
            " fallback, until recent plays show which add-on is fastest.",
            "sources",
            choices="sources",
        ),
        "bulk_downloads": Meta(
            "Songs fetched at once",
            "Whole songs fetched at the same time. More wait their turn, and playback goes"
            " first. 0: no limit.",
            "bulk",
            unit="songs",
        ),
        "addon_downloads": Meta(
            "Songs fetched per add-on",
            "Whole songs fetched from one add-on at the same time. Add-ons that share an"
            " account or worker share this limit; set exceptions and shared groups on the"
            " Add-ons page. 0: no limit.",
            "bulk",
            unit="songs",
        ),
        "addon_preparations": Meta(
            "New preparations per add-on",
            "Songs one add-on may be asked to prepare at the same time, for downloads and"
            " playlists, when it hasn't confirmed their audio is ready. Each counts until the"
            " add-on starts sending audio; for an add-on without a cache check, every song"
            " does. Add-ons that share an account or worker share this limit. 0: no limit.",
            "bulk_more",
            unit="songs",
        ),
        "budget_seconds": Meta(
            "Time to first audio",
            "How long a song may take to start, including one fallback. With Primary first,"
            " the primary may use all of it and the fallbacks get their own. An add-on can"
            " have its own time on the Add-ons page.",
            "sources",
            cross="Must be at most the longest wait ({max_wait_seconds} s), or apps give up"
            " before an add-on has had its time.",
            cross_notice="is above the longest wait.",
        ),
        "dash": Meta(
            "Play DASH links",
            "Off: an add-on's DASH link is not played, and the next add-on is asked.",
            "dash",
        ),
        "quality_default": Meta(
            "Lowest quality for listeners",
            "Compressed audio (MP3, AAC, Opus…) needs at least this bitrate and 44.1 kHz."
            " Lossless audio (FLAC, ALAC…) needs only the 44.1 kHz, and with Lossless nothing"
            " else is accepted. A song whose file is below it plays from the add-ons when"
            " they have it, and such a file does not play for a catalog song. Each person can"
            " choose their own on their Audio quality page; this applies to those who haven't.",
            "sources",
            options={
                "any": "Any",
                "128": "Compressed 128 kb/s",
                "160": "Compressed 160 kb/s",
                "192": "Compressed 192 kb/s",
                "256": "Compressed 256 kb/s",
                "320": "Compressed 320 kb/s",
                "lossless": "Lossless only",
            },
        ),
        "dash_quality_from": Meta(
            "DASH quality: from",
            "The highest quality a stream offers within this range is played. A stream with"
            " none in the range is not played, and the next add-on is asked.",
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
            "With Primary first: how long the primary add-on has to start the audio before"
            " an add-on that has the song ready takes over.",
            "timing",
        ),
        "max_wait_seconds": Meta(
            "Longest wait",
            "The most an app waits for a song's first audio, whatever the routing and the"
            " add-ons' own times. Keep it below the apps' own timeouts.",
            "timing",
            cross="Must be at least the time to first audio ({budget_seconds} s), or apps give"
            " up before an add-on has had its time.",
            cross_notice="is below the time to first audio.",
        ),
        "availability_timeout_seconds": Meta(
            "Ready check timeout",
            "How long add-ons have to answer whether they have a song ready.",
            "timing",
        ),
        "reliable_lookup_after_seconds": Meta(
            "Fallback lookup after",
            "With Primary first: once the primary has taken this long, the likely fallback"
            " also looks the song up, without playing it. 0: with every play.",
            "timing",
        ),
        "seek_timeout_seconds": Meta(
            "Seek timeout", "How long a jump within a playing song may take.", "timing"
        ),
        "ahead_window_seconds": Meta(
            "Fetch-ahead detection",
            "Some apps fetch the next songs as soon as one starts. A song requested this soon"
            " after another on the same app, and not reported as playing, counts as fetched"
            " ahead: such songs are looked up one at a time, after the song being played has"
            " started. 0: off.",
            "timing",
        ),
        "prepare_when_not_ready": Meta(
            "Prepare for next time",
            "With Ready first: when no add-on has a song ready, one of them is asked to"
            " prepare it for the next play.",
            "fallbacks",
        ),
        "max_attempts": Meta(
            "Attempts per play",
            "How many add-ons get their time to start a song. With Primary first, these are"
            " the fallbacks after the primary. An add-on that lacks the song, or fails at"
            " once, doesn't count.",
            "fallbacks",
            unit="attempts",
        ),
        "length_tolerance_seconds": Meta(
            "Length difference allowed",
            "Audio whose length differs from the catalog's by more than this (or by the share"
            " below, if that is larger) is taken as another recording, and the next add-on is"
            " asked. Both 0: no check.",
            "fallbacks",
        ),
        "length_tolerance_percent": Meta(
            "Length difference as a share",
            "The same limit, as a share of the song's length.",
            "fallbacks",
            unit="%",
            spoken="percent",
        ),
        "cooldown_seconds": Meta(
            "Cooldown",
            "How long an add-on that cools down is passed over.",
            "cooldown",
        ),
        "cooldown_errors": Meta(
            "Errors before cooldown",
            "An add-on that fails this many times in a row (server errors, no connection,"
            " broken answers) cools down, and again after each further error until it"
            " delivers audio. A missing song or a slow answer is not an error. 0: off.",
            "cooldown",
            unit="errors",
        ),
        "primary_cooldown_timeouts": Meta(
            "Timeouts before cooldown",
            "The primary add-on cools down after this many timeouts with no audio delivered"
            " in between.",
            "cooldown",
            unit="timeouts",
        ),
        "primary_cooldown_switches": Meta(
            "Switches before cooldown",
            "It also cools down after another add-on that had the song ready has taken over"
            " from it this many times.",
            "cooldown",
            unit="switches",
        ),
        "primary_miss_hours": Meta(
            "Songs the primary lacks",
            "A song the primary add-on didn't have isn't asked of it again for this long. 0: off.",
            "cooldown",
        ),
        "primary_release_miss_minutes": Meta(
            "Albums the primary lacks",
            "When the primary lacks one song of an album, the album's other songs are also"
            " looked up at the fallback right away, for this long. The primary still starts"
            " first. 0: off.",
            "cooldown",
        ),
        "retry_skip_seconds": Meta(
            "Retries skip failed add-ons",
            "When an app retries a song that just failed, the add-ons that failed or lacked"
            " it within this time are skipped. 0: every retry asks them all.",
            "cooldown",
        ),
        "warm_ahead_depth": Meta(
            "Next songs opened",
            "How many of the next songs are opened when one starts playing. 0: off.",
            "warm",
            unit="songs",
        ),
        "warm_ahead_jobs": Meta(
            "Plays at once",
            "How many playing songs have their next songs opened at the same time, for all"
            " listeners together. More wait their turn; songs being played always go first.",
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
            "An app that fetches the next songs itself gets none opened for it, for this long"
            " after it last did.",
            "warm",
        ),
        "download_timeout_seconds": Meta(
            "Download timeout", "How long fetching one whole song may take.", "downloads"
        ),
        "keep_streamed_audio": Meta(
            "Keep streamed songs",
            "When Shijhon has received a song's whole audio while an app streams it, the song"
            " replaces its silent placeholder, as a downloaded song does. Nothing more is"
            " fetched to finish a song: one skipped before Shijhon had all of it isn't kept."
            " The limits below apply to it too.",
            "downloads",
        ),
        "delivered_days": Meta(
            "Kept unused for",
            "A saved song goes back to its silent placeholder after this many days unused."
            " 0: no age limit.",
            "downloads",
        ),
        "delivered_gb": Meta(
            "Size limit",
            "When saved songs take more than this in all, the least recently used go back"
            " first. 0: no size limit.",
            "downloads",
            unit="GB",
            spoken="gigabytes",
        ),
        "user_routings": Meta(
            "Songs looked up per user",
            "Songs one user's apps look up at the add-ons at the same time. 0: no limit.",
            "limits",
            unit="songs",
        ),
        "user_downloads": Meta(
            "Songs fetched per user",
            "Whole songs fetched for one user at the same time: app downloads, playlist"
            " preparation and plays that need converting. Ordinary streaming doesn't count."
            " 0: no limit.",
            "limits",
            unit="songs",
        ),
        "user_downloads_per_hour": Meta(
            "Hourly download quota",
            "Whole songs one user may start fetching in an hour. A share link counts as a"
            " user. This counts Shijhon's own fetches, not what an add-on account has left."
            " 0: off.",
            "limits",
            unit="songs",
        ),
        "user_download_burst": Meta(
            "Quota burst",
            "With an hourly quota: how many fetches may start right away after a quiet while;"
            " the rest are spread evenly over the hour. 0: the whole hour's quota at once."
            " Ignored while the quota is off.",
            "limits",
            unit="songs",
        ),
        "addon_requests_per_second": Meta(
            "Requests a second",
            "Requests sent to one add-on each second: lookups, links and ready checks. More"
            " wait their turn. 0: no limit.",
            "addons",
            unit="/s",
            spoken="requests a second",
        ),
        "addon_request_burst": Meta(
            "Requests at once",
            "How many requests may go at once after a quiet moment, before the rate above applies.",
            "addons",
            unit="requests",
        ),
        "addon_audio_openings": Meta(
            "Songs opened at once",
            "Songs whose audio one add-on is asked for at the same time. The song being"
            " played never waits. 0: no limit.",
            "addons",
            unit="songs",
        ),
        "dash_start": Meta(
            "DASH songs start",
            "At once: a song plays from its first segments. When joined: it plays once all its"
            " segments are joined into one file. Songs saved to the library are joined files"
            " either way.",
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
            "DASH songs fetched and joined at the same time, for all listeners together. More"
            " wait their turn; the songs being played never wait.",
            "joining",
            unit="songs",
        ),
        "dash_cache_mb": Meta(
            "Joined files kept",
            "Space for joined files, kept for later plays and seeks. Past this size, the least"
            " recently used go first. 0: only the newest is kept.",
            "joining",
        ),
        "request_timeout_seconds": Meta(
            "Add-on request timeout", "How long one request to an add-on may take.", "advanced"
        ),
        "pin_ttl_seconds": Meta(
            "Link reuse",
            "How long a song keeps the add-on and link it played from, for seeks and repeats.",
            "advanced",
        ),
    },
    "fill": {
        "auto_min_songs": Meta(
            "At least this many songs",
            "A partly owned album is filled automatically when you own at least this many of"
            " its songs.",
            "policy",
            unit="songs",
        ),
        "auto_min_share": Meta(
            "At least this share of the album",
            "Or when you own at least this share of its songs.",
            "policy",
            unit="%",
            spoken="percent",
            percent=True,
        ),
        "library_pass": Meta(
            "Library pass",
            "Checks every album against the catalog in the background. Dry run fills nothing"
            " automatically and lists what it would fill. On fills the albums that meet the"
            " fill policy. Off: albums are matched when an app views them.",
            "pass",
            options={"off": "Off", "dry_run": "Dry run", "on": "On"},
        ),
        "enabled": Meta(
            "Fill partly owned albums",
            "Off: partly owned albums are not matched with the catalog, shown complete or"
            " filled, and the Library page lists nothing.",
            "albums",
        ),
        "complete_lists": Meta(
            "Complete albums in lists",
            "Album lists and searches also show a partly owned album's complete song count"
            " and length. Off: they show the songs you own until the album is filled.",
            "albums",
        ),
        "auto_pick": Meta(
            "Pick a release when several fit",
            "When several catalog releases fit an album equally well, or the one that fits"
            " numbers its tracks differently, the most likely is used and listed under Picked"
            " automatically. Off: such albums wait in the review list.",
            "albums",
        ),
        "open_budget_seconds": Meta(
            "Wait for a match on first view",
            "How long an album opened for the first time waits for its catalog match. After"
            " that it shows only your songs, and the match goes on in the background.",
            "matching",
        ),
        "sync_albums": Meta(
            "Albums before background matching",
            "An app that opens this many unmatched albums within the time below is syncing:"
            " those albums show your songs at once and are matched in the background.",
            "matching",
            unit="albums",
        ),
        "sync_seconds": Meta("Counted within", "The time window for the count above.", "matching"),
        "retry_hours": Meta(
            "Retry after a failure",
            "A match that failed is tried again after this long, then after twice as long"
            " each time.",
            "matching",
        ),
        "background_pause_seconds": Meta(
            "Pause between background matches",
            "The pause between albums matched in the background, and after each fill the"
            " library pass makes.",
            "matching",
        ),
        "pass_start_seconds": Meta(
            "First pass after",
            "How long after Shijhon starts the library pass first runs.",
            "advanced",
        ),
        "pass_interval_hours": Meta(
            "Pass repeats every",
            "How often the library pass looks again for new albums and failed matches.",
            "advanced",
        ),
        "pass_requests_per_second": Meta(
            "Catalog requests per second",
            "The most requests the library pass sends the catalog each second.",
            "advanced",
            unit="per s",
            spoken="requests per second",
        ),
    },
    "search": {
        "budget_seconds": Meta(
            "Wait for the catalog",
            "How long a search or an artist page waits for the catalog. The answer goes out"
            " as soon as the catalog replies; after this time, your library's results are"
            " shown alone. The lookup goes on, so the same search asked again has them.",
            "settings",
        ),
    },
    "cleanup": {
        "mode": Meta(
            "Cleanup",
            "Once a day, whole releases that Shijhon added and nobody has used are taken out"
            " of the library again. Dry run only lists them. A release taken out comes back"
            " when one of its songs is used.",
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
            "Catalog albums added to your library when one of their songs was used.",
            "cleanup",
        ),
        "fills": Meta(
            "Take out songs added to your albums",
            "The songs added to complete a partly owned album. Once they are taken out, the"
            " album is still shown complete, and is filled again on its next use, not"
            " automatically.",
            "cleanup",
        ),
    },
    "audiomuse": {
        "url": Meta(
            "AudioMuse-AI address",
            "Where AudioMuse-AI answers, for example http://audiomuse:8000. Empty: sound"
            " analysis is off.",
            "connection",
        ),
        "token": Meta(
            "API token",
            "AudioMuse-AI's API token. " + _WRITE_ONLY + " It is sent only to the address above.",
            "connection",
        ),
        "server": Meta(
            "Server name in AudioMuse-AI",
            "The name or ID AudioMuse-AI gives this Shijhon among its media servers. Empty: its"
            " default server.",
            "connection",
        ),
        "reach": Meta(
            "Where AudioMuse-AI runs",
            "An address on this machine or your local network is refused unless chosen here."
            " In a container, the Docker host is on the local network.",
            "connection",
            options={"public": "Internet", "private": "Local network", "loopback": "This machine"},
        ),
        "timeout_seconds": Meta(
            "Request timeout", "How long one request to AudioMuse-AI may take.", "advanced"
        ),
        "sync_hours": Meta(
            "Read the analysis every",
            "How often Shijhon reads AudioMuse-AI's analysis of your songs. It also reads it"
            " once after starting, if the last read is older than this.",
            "advanced",
        ),
    },
    "catalog": {
        # Its choices are the installed catalog adapters; an adapter's own settings follow
        # it, with the words the adapter declares (``sections.fields_of``).
        "kind": Meta(
            "Catalog",
            "Where searches, artist pages and album track lists get the music beyond your"
            ' own: an installed catalog adapter, or "An add-on\'s catalog" to use one of'
            " your add-ons.",
            "connection",
        ),
        "twins": Meta(
            "Clean and explicit versions",
            "Which version of an album or song is shown when the catalog has both.",
            "albums",
            options={"explicit": "Explicit", "clean": "Clean", "both": "Both"},
        ),
        "artwork_size": Meta(
            "Cover size in the library",
            "The size of the cover saved with each catalog album added to your library.",
            "covers",
            unit="px",
        ),
        "cover_sizes": Meta(
            "Cover sizes for apps",
            "A cover an app asks for is fetched at the next of these sizes up: image servers"
            " tend to answer common sizes faster. Empty: the exact size asked for.",
            "covers",
            unit="px",
        ),
        "artwork_cache_mb": Meta(
            "Cover cache",
            "Disk space for resized covers. Past this size, the oldest go first. 0: none kept"
            " on disk.",
            "covers",
        ),
        "artwork_cache_days": Meta(
            "Covers kept for", "How long a resized cover is kept on disk.", "covers"
        ),
        "prefetch_covers": Meta(
            "Covers fetched ahead",
            "How many covers of the first catalog items on an artist page or in search results"
            " are fetched in the background, before the app asks for them. 0: none.",
            "covers",
        ),
        "prefetch_parallel": Meta(
            "Covers fetched at once",
            "How many of those covers are fetched at the same time.",
            "covers",
        ),
        "prefetch_burst_pages": Meta(
            "Pages before the pause",
            "An app shown this many pages with catalog items within the time below is paging"
            " through them: no covers are fetched ahead for it meanwhile.",
            "covers",
        ),
        "prefetch_burst_seconds": Meta(
            "Pages counted within", "The time window for the count above.", "covers"
        ),
        "cache_seconds": Meta(
            "Answers reused for", "How long a catalog answer is kept and reused.", "advanced"
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


def counted(number: str, unit: str) -> str:
    """A number and its unit, a counted noun in the singular for one: "1 song", "4 songs",
    "1 switch", "2 /s", "30 s"."""
    if number == "1" and unit.isalpha() and len(unit) > 3 and unit.endswith("s"):
        unit = unit[:-2] if unit.endswith(("ches", "shes", "sses", "xes")) else unit[:-1]
    return f"{number} {unit}".strip()


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
            return counted(format_number(self._shown(value)), self.unit)
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
