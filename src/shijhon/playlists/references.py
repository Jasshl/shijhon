"""The playlists and songs a description relates to: named in the request, or the caller's
own playlists whose names the instructions mention. They are read through the caller's
Navidrome connection at every generation, so refreshes follow their current songs."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio
from pydantic import ValidationError

from shijhon.playlists.model import (
    LIBRARY_LOST,
    Connection,
    Definition,
    PlaylistError,
    Track,
    is_text,
)
from shijhon.playlists.resolve import native_track

if TYPE_CHECKING:
    from shijhon.playlists.service import Playlists

READ = 200  # reference songs kept for curation
SHOWN = 100  # of those, shown to the AI with their facts
DETECTED = 3
OWNED = 5000  # owned playlist names compared with the instructions
# Words that alone make a name too general to stand for one playlist.
GENERIC = frozenset(
    """
    a an and the my our your of for in on with to from by new old all best top more less
    playlist playlists list mix mixes music song songs track tracks tune tunes album albums
    favorite favorites favourite favourites liked loved starred saved untitled radio
    described discovery daily weekly chill focus workout party energy relax relaxing sleep
    study running drive driving morning evening night late day summer winter spring autumn
    fall happy sad calm mellow upbeat dance rock pop jazz blues soul funk folk country
    classical electronic house techno ambient indie metal punk rap hip hop
    """.split()  # noqa: SIM905
)
_QUOTE = "[\"'\u201c\u201d\u2018\u2019\u00ab\u00bb]"


def mention(name: str) -> str:
    """A whole-word mention; a one-word name counts only when it is quoted or called a
    playlist or mix, so ordinary words of the instructions are not taken for names."""
    word = rf"(?<!\w){re.escape(name)}(?!\w)"
    if " " in name:
        return word
    called = r"(?:playlist|mix)"
    return rf"(?:{_QUOTE}{word}{_QUOTE}|{word}\s+{called}\b|\b{called}\s+{word})"


def evenly[T](values: Sequence[T], limit: int) -> list[T]:
    """At most ``limit`` values spread evenly over ``values``, order kept."""
    if len(values) <= limit:
        return list(values)
    return [values[i * len(values) // limit] for i in range(limit)]


def plain(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def specific(name: str) -> bool:
    words = re.findall(r"\w+", name)
    return len("".join(words)) >= 4 and any(w not in GENERIC for w in words)


def detect(instructions: str, playlists: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The playlists whose names the instructions mention as whole words, without case;
    longer names first, a shorter name inside a longer one's mention not counted, at most
    three. Of playlists with the same name, the most recently changed."""
    text = plain(instructions)
    named: dict[str, list[dict[str, Any]]] = {}
    for playlist in playlists:
        name = plain(str(playlist.get("name", "")))
        if specific(name):
            named.setdefault(name, []).append(playlist)
    taken: list[tuple[int, int]] = []
    found: list[dict[str, Any]] = []
    for name in sorted(named, key=lambda n: (-len(n), n)):
        spans = [
            m.span()
            for m in re.finditer(mention(name), text)
            if not any(m.start() < end and start < m.end() for start, end in taken)
        ]
        if not spans:
            continue
        taken.extend(spans)
        found.append(max(named[name], key=lambda p: str(p.get("changed", ""))))
        if len(found) == DETECTED:
            break
    return found


@dataclass
class Reference:
    id: str
    name: str
    songs: int
    detected: bool


@dataclass
class References:
    playlists: list[Reference] = field(default_factory=list)
    songs: list[Track] = field(default_factory=list)  # read, explicit songs first
    explicit_songs: int = 0
    # Every song of the references the caller chose, for leaving them out; songs of
    # playlists only named in the instructions are shown to the AI but not removed.
    ids: set[str] = field(default_factory=set)
    keys: set[str] = field(default_factory=set)
    # Every reference song, chosen or named: sound suggestions look beyond them.
    every: set[str] = field(default_factory=set)

    def shown(self) -> list[Track]:
        return evenly(self.songs, SHOWN)

    def contains(self, track: Track) -> bool:
        return track.id in self.ids or track.key in self.keys

    def summary(self, exclude: bool) -> dict[str, Any]:
        return {
            "playlists": [
                {"id": p.id, "name": p.name, "songs": p.songs, "detected": p.detected}
                for p in self.playlists
            ],
            "songs": self.explicit_songs,
            "shown": len(self.shown()),
            "exclude": exclude,
        }


def entries(data: Any) -> list[Track]:
    songs = []
    for entry in data if isinstance(data, list) else []:
        if isinstance(entry, dict) and entry.get("id"):
            try:
                songs.append(native_track(entry))
            except (ValidationError, ValueError, TypeError):
                continue  # an untagged file names no artist or title
    return songs


def visible(name: Any) -> str:
    text = " ".join(str(name or "").split())[:100]
    return text if is_text(text) else ""


async def playlist(
    service: Playlists, connection: Connection, ident: str
) -> tuple[str, list[Track]]:
    reply = await service.client.call(connection, "getPlaylist", [("id", ident)])
    data = reply.get("playlist")
    if not isinstance(data, dict) or data.get("id") != ident:
        raise PlaylistError("Navidrome did not return the playlist.")
    return visible(data.get("name")), entries(data.get("entry"))


async def read(
    service: Playlists,
    connection: Connection,
    definition: Definition,
    *,
    own: str = "",
    allowed: Callable[[], Awaitable[bool]] | None = None,
    check: Callable[[], Awaitable[None]],
) -> References:
    """Explicit references must all be readable; a detected one that is not is skipped.
    ``own``: the playlist this description publishes, never its own detected reference.
    ``allowed``: whether the caller may now read the library beyond what it names (an
    agent's library permission; None: the owner). Without it no playlist is listed or
    read for names in the instructions; losing it before such a read stops the run."""
    refs = References()
    named: list[tuple[str, str, bool]] = [(i, "", False) for i in definition.reference_playlists]
    if not definition.references and (allowed is None or await allowed()):
        await check()
        try:
            listing = await service.client.call(connection, "getPlaylists")
        except PlaylistError:
            listing = {}
        found = listing.get("playlists", {})
        owned = [
            p
            for p in (found.get("playlist", []) if isinstance(found, dict) else [])
            if isinstance(p, dict)
            and p.get("owner") == connection.username
            and p.get("id")
            and p.get("id") != own
        ][:OWNED]
        detected = detect(definition.brief, owned)
        named = [(str(p["id"]), visible(p.get("name")), True) for p in detected]
    pooled: list[Track] = []
    chosen_lists: list[Track] = []
    for ident, name, found_by_name in named:
        await check()
        # Each read of a playlist found by name needs the permission at that moment.
        if found_by_name and allowed is not None and not await allowed():
            raise PlaylistError(LIBRARY_LOST)
        try:
            title, listed = await playlist(service, connection, ident)
        except PlaylistError:
            if found_by_name:
                continue
            raise PlaylistError(
                f'The reference playlist "{ident}" is unavailable to this account.'
            ) from None
        refs.playlists.append(Reference(ident, title or name, len(listed), found_by_name))
        pooled.extend(listed)
        if not found_by_name:
            chosen_lists.extend(listed)
    chosen = await read_songs(service, connection, definition.reference_songs, check)
    refs.explicit_songs = len(chosen)
    refs.every.update(t.id for t in [*chosen, *pooled])
    for track in [*chosen, *chosen_lists]:
        refs.ids.add(track.id)
        refs.keys.add(track.key)
    unique: dict[str, Track] = {}
    for track in chosen:
        unique.setdefault(track.key, track)
    rest: dict[str, Track] = {}
    for track in pooled:
        if track.key not in unique:
            rest.setdefault(track.key, track)
    refs.songs = [*unique.values(), *evenly(list(rest.values()), max(0, READ - len(unique)))]
    return refs


async def read_songs(
    service: Playlists,
    connection: Connection,
    idents: Sequence[str],
    check: Callable[[], Awaitable[None]],
) -> list[Track]:
    if not idents:
        return []
    await check()
    found: dict[str, Track] = {}
    missing: list[str] = []
    limiter = anyio.CapacityLimiter(4)

    async def one(ident: str) -> None:
        async with limiter:
            try:
                reply = await service.client.call(connection, "getSong", [("id", ident)])
                track = native_track(reply["song"])
                if track.id != ident:
                    raise ValueError
                found[ident] = track
            except (PlaylistError, KeyError, TypeError, ValueError):
                missing.append(ident)

    async with anyio.create_task_group() as group:
        for ident in idents:
            group.start_soon(one, ident)
    if missing:
        first = next(i for i in idents if i in missing)
        raise PlaylistError(f'The reference song "{first}" is unavailable to this account.')
    await check()
    return [found[i] for i in idents]
