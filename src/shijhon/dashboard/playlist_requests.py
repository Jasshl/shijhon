"""Standalone playlist requests and explicit agent grants in the CSRF dashboard."""

from __future__ import annotations

import re
import secrets
import time
from typing import TYPE_CHECKING, Any

import anyio
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from shijhon.dashboard.auth import Session
from shijhon.playlists.api import API
from shijhon.playlists.edits import Edits, remembered
from shijhon.playlists.model import REFERENCE_PLAYLISTS, Definition, PlaylistError, Track
from shijhon.playlists.references import plain, visible
from shijhon.playlists.requests import PlaylistRequest

if TYPE_CHECKING:
    from shijhon.dashboard.app import Dashboard
    from shijhon.playlists.service import Playlists

CHOICES = 500  # own playlists offered as references, by name
CHOICE_SECONDS = 5  # reading them for the form
EDIT_SONGS = 20  # remembered songs listed, of each kind
REFERENCE_FIELDS = [f"reference_playlist_{n}" for n in range(1, REFERENCE_PLAYLISTS + 1)]
FITS = {"meets": "Fits", "partly": "Partly fits", "misses": "Doesn't fit"}
SOURCES = {"own": "Your files", "added": "Added by Shijhon", "catalog": "From the catalog"}
SUGGESTED = {
    "sound_similar": "Sounds like the reference",
    "sound_search": "Matches the described sound",
}
REVISIONS = {
    "revised": "Revised once: the AI looked again at the songs it rated as not fully"
    " fitting and changed the selection.",
    "kept": "Checked once more: the AI looked again at the songs it rated as not fully"
    " fitting; the first selection stayed.",
}


def text(value: Any, limit: int = 300) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def number(value: Any, low: int = 0, high: int = 100_000) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return round(value) if low <= value <= high else None


def texts(value: Any, limit: int) -> list[str]:
    found = [t for v in value if (t := text(v, 40))] if isinstance(value, list) else []
    return found[:limit]


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def listed(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def label(names: dict[str, str], value: Any) -> str:
    return names.get(value, "") if isinstance(value, str) else ""


def bar(percentile: int) -> float:
    """A percentile's place on the energy bar (0.5 to 99.5 of its 100 units)."""
    return round(0.5 + percentile * 0.99, 1)


def song_view(song: dict[str, Any]) -> dict[str, Any]:
    """One selected song as the request page shows it; missing facts are left out."""
    facts = mapping(song.get("facts"))
    sound = mapping(facts.get("sound"))
    details = [str(year)] if (year := number(facts.get("year"), 1, 9999)) else []
    if genres := texts(facts.get("genres"), 3):
        details.append(", ".join(genres))
    if tags := texts(facts.get("tags"), 5):
        details.append("tags: " + ", ".join(tags))
    if key := text(sound.get("key"), 20):
        details.append(key)
    tempo = number(sound.get("tempo"), 1, 1000)
    energy = number(sound.get("energy"), 0, 100)
    return {
        "title": text(song.get("title")),
        "artist": text(song.get("artist")),
        "album": text(song.get("album")),
        "reason": text(song.get("reason"), 500),
        "fit": label(FITS, song.get("fit")),
        "fit_key": song.get("fit") if label(FITS, song.get("fit")) else "",
        "source": label(SOURCES, song.get("source")),
        "suggested": label(SUGGESTED, facts.get("suggested_by")),
        "details": details,
        "energy": energy,
        "energy_width": round(max(energy, 1) * 0.99, 1) if energy is not None else 0,
        "tempo": tempo,
    }


def curation(shown: dict[str, Any]) -> dict[str, Any]:
    """What a run says about its curation, checked and in display words. Runs from before
    curation (or explicit tracks) have none of it: the page then shows only the songs."""
    refs = mapping(shown.get("references"))
    playlists = [
        {
            "name": text(p.get("name"), 100) or "Untitled playlist",
            "songs": number(p.get("songs")) or 0,
            "detected": p.get("detected") is True,
        }
        for p in listed(refs.get("playlists"))
    ][:REFERENCE_PLAYLISTS]
    profile = mapping(shown.get("reference_profile"))
    energy = number(profile.get("energy"), 0, 100)
    reference = {
        "energy": energy,
        # (The line stays whole at either end of the bar.)
        "marker": min(max(bar(energy), 1.5), 98.5) if energy is not None else 0,
        "tempo": number(profile.get("tempo"), 1, 1000),
    }
    summary = []
    if reference["energy"] is not None:
        summary.append(f"energy {reference['energy']}")
    if reference["tempo"]:
        summary.append(f"{reference['tempo']} BPM")
    years = profile.get("years")
    if isinstance(years, list) and len(years) == 2:
        first, last = (number(y, 1, 9999) for y in years)
        if first and last:
            summary.append(str(first) if first == last else f"{first}\u2013{last}")
    if genres := texts(profile.get("genres"), 5):
        summary.append("genres: " + ", ".join(genres))
    if tags := texts(profile.get("tags"), 5):
        summary.append("tags: " + ", ".join(tags))
    coverage = mapping(shown.get("coverage"))
    states = {k: coverage.get(k) for k in ("sound", "tags")}
    songs = [song_view(s) for s in listed(shown.get("selected"))]
    counted = number(coverage.get("songs"))
    facts = []
    for state, key, words in (
        ("sound", "with_sound", "sound facts"),
        ("tags", "with_tags", "Last.fm tags"),
    ):
        if counted and states[state] in ("used", "unavailable"):
            have = number(coverage.get(key)) or 0
            facts.append(f"{have} of {counted} songs {'has' if have == 1 else 'have'} {words}")
    pool = mapping(shown.get("sound_pool"))
    revision = shown.get("revision")
    return {
        "playlists": playlists,
        "chosen": any(not p["detected"] for p in playlists),
        "detected": any(p["detected"] for p in playlists),
        "songs_chosen": number(refs.get("songs")) or 0,
        "exclude": refs.get("exclude") is not False,
        "left_out": number(shown.get("left_out")) or 0,
        "reference": reference,
        "summary": summary,
        "reference_sound": (
            (number(profile.get("with_sound")) or 0, number(profile.get("songs")) or 0)
            if states["sound"] in ("used", "unavailable") and number(profile.get("songs"))
            else None
        ),
        "goal": text(shown.get("goal")),
        "revision": label(REVISIONS, revision),
        "facts": facts,
        "sound_unavailable": states["sound"] == "unavailable",
        "tags_unavailable": states["tags"] == "unavailable",
        "sound_suggested": sum(number(v) or 0 for v in pool.values()),
        "songs": songs,
        "energy": any(s["energy"] is not None for s in songs),
    }


def count(number_of: int, word: str) -> str:
    return f"{number_of} {word}{'' if number_of == 1 else 's'}"


def edit_songs(found: list[Track]) -> dict[str, Any]:
    """Remembered songs as the page lists them: the latest few, then how many more."""
    return {
        "songs": [
            " · ".join(p for p in (text(t.title, 150), text(t.artist, 150)) if p)
            for t in found[:EDIT_SONGS]
        ],
        "more": max(len(found) - EDIT_SONGS, 0),
    }


def edits_view(run_edits: Any, kept: Edits) -> dict[str, Any] | None:
    """What refreshes learned from the owner's edits of their playlist, in display words:
    the songs remembered now (``kept``, which the forget button clears) and, from the run
    shown, what it noticed and how many suggestions it left out. Nothing when there is
    nothing to say, as for a one-off request or a run from before refreshes learned."""
    shown = mapping(run_edits)
    learned = mapping(shown.get("learned"))
    noticed = [len(listed(learned.get(k))) for k in ("removed", "added")]
    left_out = number(shown.get("left_out")) or 0
    removed, added = len(kept.removed), len(kept.added)
    if not (removed or added or any(noticed) or left_out):
        return None
    parts = []
    if removed:
        parts.append(f"{count(removed, 'removed song')} {'stays' if removed == 1 else 'stay'} out")
    if added:
        parts.append(f"{count(added, 'added song')} {'guides' if added == 1 else 'guide'} the AI")
    seen = [
        f"{verb} {count(n, 'song')}"
        for verb, n in zip(("removed", "added"), noticed, strict=True)
        if n
    ]
    return {
        "summary": ", ".join(parts) or "nothing is remembered for this playlist now",
        "removed": edit_songs(kept.removed),
        "added": edit_songs(kept.added),
        "remembered": bool(removed or added),
        "noticed": (
            f"Since the playlist was last published, you {' and '.join(seen)}." if seen else ""
        ),
        "left_out": (
            f"{count(left_out, 'suggested song')} {'was' if left_out == 1 else 'were'} left out"
            " of this run because you removed them."
            if left_out
            else ""
        ),
    }


class Pages:
    def __init__(self, dashboard: Dashboard) -> None:
        self.dashboard = dashboard

    @property
    def service(self) -> Playlists:
        if not self.dashboard.services or not self.dashboard.services.playlists:
            raise PlaylistError("Playlist services are starting.")
        return self.dashboard.services.playlists

    def routes(self) -> list[Route]:
        page = self.dashboard._personal_page
        return [
            Route("/playlists/describe", page(self.index), methods=["GET", "POST"]),
            Route("/playlists/agents", page(self.agents), methods=["POST"]),
            Route("/playlists/describe/{ident}", page(self.detail), methods=["GET", "POST"]),
            *API(lambda: self.service).routes(),
        ]

    async def choices(self, session: Session) -> dict[str, Any]:
        """The account's own playlists, by name, for choosing references: read through its
        connection like the references themselves, bounded in number and time."""
        service = self.service
        try:
            with anyio.fail_after(CHOICE_SECONDS):
                connection = await service.connection(session.user_id)
                reply = await service.client.call(connection, "getPlaylists")
        except (PlaylistError, TimeoutError):
            return {"playlists": [], "more": False, "unavailable": True}
        found = mapping(reply.get("playlists")).get("playlist")
        own = [
            {
                "id": p["id"],
                "name": visible(p.get("name")) or "Untitled playlist",
                "songs": number(p.get("songCount")) or 0,
            }
            for p in listed(found)
            if p.get("owner") == connection.username
            and isinstance(p.get("id"), str)
            and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", p["id"])
        ]
        own.sort(key=lambda p: (plain(p["name"]), p["id"]))
        return {"playlists": own[:CHOICES], "more": len(own) > CHOICES, "unavailable": False}

    async def render(
        self,
        session: Session,
        *,
        ident: str = "",
        error: str = "",
        form: dict[str, str] | None = None,
    ) -> Response:
        service = self.service
        connection = await service.repo.connection(session.user_id)
        rows = await service.repo.store.fetchall(
            "SELECT m.* FROM playlist_mixes m JOIN playlist_requests r ON r.id=m.id"
            " WHERE m.owner=? ORDER BY m.rowid DESC LIMIT 100",
            [session.user_id],
        )
        listing = [
            {**dict(r), "spec": Definition.model_validate_json(r["definition"])} for r in rows
        ]
        shown: dict[str, Any] | None = None
        run = None
        spec = None
        learning = None
        if ident:
            try:
                shown = await service.requests.status(session.user_id, ident)
                run = await service.repo.run(shown["run"])
                mix = await service.repo.mix(ident, session.user_id)
                spec = Definition.model_validate_json(mix["definition"])
                learning = edits_view(shown["edits"], await remembered(service.repo.store, ident))
            except PlaylistError as exc:
                error = error or str(exc)
        references = (
            await self.choices(session)
            if connection and not ident
            else {"playlists": [], "more": False, "unavailable": False}
        )
        return await self.dashboard._render(
            "playlist_requests.html",
            session=session,
            current="playlists",
            error=error,
            connected=bool(connection),
            listing=listing,
            shown=shown,
            curation=curation(shown) if shown else None,
            learning=learning,
            run=run,
            spec=spec,
            agents=await service.requests.tokens(session.user_id),
            request_key=secrets.token_hex(16),
            form=form or {},
            references=references,
            reference_fields=REFERENCE_FIELDS,
        )

    async def index(self, request: Request, session: Session) -> Response:
        if request.method != "POST":
            return await self.render(session)
        try:
            form = request.state.form
            spec = PlaylistRequest(
                name=form.get("name", ""),
                prompt=form.get("prompt", ""),
                count=int(form.get("count", "30")),
                max_per_artist=int(form.get("max_per_artist", "2")),
                use_history=form.get("use_history") == "true",
                use_library=form.get("use_library") == "true",
                save_instructions=form.get("save_instructions") == "true",
                cadence=form.get("cadence", "manual"),
                preview=form.get("preview") == "true",
                # The same playlist chosen twice counts once.
                reference_playlists=list(
                    dict.fromkeys(v for f in REFERENCE_FIELDS if (v := form.get(f, "")))
                ),
                # A hidden "false" precedes the box: missing means the default.
                exclude_reference=form.get("exclude_reference", "true") == "true",
            )
            async with self.service.changes:
                ident, _ = await self.service.requests.create(
                    session.user_id, form.get("request_key", ""), spec
                )
            return self.dashboard._see(f"/playlists/describe/{ident}")
        except (PlaylistError, ValidationError, ValueError) as exc:
            error = (
                str(exc)
                if isinstance(exc, PlaylistError)
                else (
                    "Check the instructions, count, artist limit and reference playlists. "
                    "Refreshes need saved instructions."
                )
            )
            return await self.render(session, error=error, form=request.state.form)

    async def agents(self, request: Request, session: Session) -> Response:
        try:
            async with self.service.changes:
                form = request.state.form
                if form.get("action") == "revoke":
                    await self.service.requests.revoke(session.user_id, form.get("agent", ""))
                    return self.dashboard._see("/playlists/describe")
                raise PlaylistError("Connect agents through OAuth at /shijhon/mcp.")
        except PlaylistError as exc:
            return await self.render(session, error=str(exc))

    async def detail(self, request: Request, session: Session) -> Response:
        ident = request.path_params["ident"]
        if request.method != "POST":
            return await self.render(session, ident=ident)
        try:
            service = self.service
            async with service.changes:
                await service.requests.owned(session.user_id, ident, None)
                mix = await service.repo.mix(ident, session.user_id)
                if request.state.form.get("action") == "forget_edits":
                    # The owner's own record: no connection or agent grant is needed.
                    await service.requests.forget_edits(session.user_id, ident)
                    return self.dashboard._see(f"/playlists/describe/{ident}")
                action = request.state.form.get("action", "")
                if action not in ("pause", "discard"):
                    await service.requests.guard(mix)
                run = (await service.repo.runs(ident))[0]
                spec = Definition.model_validate_json(mix["definition"])
                action = request.state.form.get("action", "")
                if action in ("publish", "discard", "retry") and (
                    request.state.form.get("run") != run["id"] or run["mix"] != ident
                ):
                    raise PlaylistError("This run is no longer current. Reload the page.")
                if action == "retry":
                    await service.requests.retry(session.user_id, ident)
                elif action == "publish" and run["state"] == "preview":
                    await service.repo.update(run["id"], "publishing", run["data"])
                elif action == "discard" and run["state"] == "preview":
                    await service.repo.update(run["id"], "discarded", run["data"])
                elif action == "refresh" and spec.save_instructions:
                    connection = await service.connection(session.user_id)
                    service.requests.usable_ai(connection)
                    await service.repo.enqueue(ident)
                elif action in ("pause", "enable"):
                    if action == "enable":
                        connection = await service.connection(session.user_id)
                        service.requests.usable_ai(connection)
                        if not spec.interval or not spec.save_instructions or not mix["published"]:
                            raise PlaylistError(
                                "Publish explicitly saved refresh instructions first."
                            )
                    if action == "pause":
                        await service.repo.pause(ident)
                    else:
                        await service.repo.store.execute(
                            "UPDATE playlist_mixes SET enabled=1,due=? WHERE id=?",
                            [time.time() + spec.interval, ident],
                        )
                else:
                    raise PlaylistError("This action is no longer available. Reload the page.")
                service.notify()
            return self.dashboard._see(f"/playlists/describe/{ident}")
        except PlaylistError as exc:
            return await self.render(session, ident=ident, error=str(exc))
