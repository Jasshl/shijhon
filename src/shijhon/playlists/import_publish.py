"""Bounded native import publication with durable, non-replayed append intents."""

from __future__ import annotations

import contextlib
import json
import secrets
import time
from typing import TYPE_CHECKING, Any

from shijhon.catalog.cache import CachedCatalog
from shijhon.catalog.model import CatalogRef
from shijhon.playlists.ai import validate
from shijhon.playlists.import_completion import CHANGED, materialize, staged_entries
from shijhon.playlists.model import Connection, Definition, PlaylistError, Track, fingerprint
from shijhon.playlists.resolve import native_track, same_recording
from shijhon.playlists.service import release_fingerprint

if TYPE_CHECKING:
    from shijhon.playlists.imports import Imports

PREPARE_BATCH = 100
APPEND_BATCH = 200


def metadata(playlist: dict[str, Any]) -> dict[str, Any]:
    return {
        **{k: playlist.get(k) or "" for k in ("id", "name", "owner", "comment")},
        "public": bool(playlist.get("public", False)),
    }


def entries(playlist: dict[str, Any]) -> list[str]:
    result = [str(s["id"]) for s in playlist.get("entry", [])]
    if "songCount" in playlist and playlist["songCount"] != len(result):
        raise PlaylistError("Navidrome did not return the complete ordered playlist.")
    return result


DIFFERS = (
    "The playlist in Navidrome differs from this review and its update. Nothing more was "
    "written. Check the playlist, stop this update and press Recheck to review it again."
)


def same_place(current: dict[str, Any], expected: dict[str, Any]) -> bool:
    """The same native playlist of the same owner. Once an update has begun writing, its
    songs decide; the name, comment and visibility are the owner's to change meanwhile."""
    found = metadata(current)
    return bool(found["id"] == expected["id"] and found["owner"] == expected["owner"])


def staged_now(data: dict[str, Any], appended: int, removed: int) -> list[str]:
    original = [s["id"] for s in data["completion"]["snapshot"]]
    prefix: int = data["import_publish"]["prefix"]
    return staged_entries(original, data["playlist_ids"], prefix, appended, removed)


class Publisher:
    def __init__(self, imports: Imports) -> None:
        self.imports, self.service = imports, imports.service

    async def guard(self, connection: Connection, mix: dict[str, Any], ident: str) -> None:
        await self.imports.running(mix, ident, "publishing")
        if not connection.same_settings(await self.service.repo.connection(connection.user_id)):
            raise PlaylistError("The connection changed. Retry with the current connection.")
        await self.service.requests.guard(mix)

    async def save(self, ident: str, data: dict[str, Any]) -> None:
        if not await self.imports.checkpoint(ident, "publishing", data):
            raise PlaylistError("This import has stopped. Published playlist entries are kept.")

    async def current(self, connection: Connection, data: dict[str, Any]) -> dict[str, Any]:
        current = await self.service.client.playlist(connection, data["created_id"])
        if current.get("id") != data["created_id"]:
            raise PlaylistError("The native playlist identity could not be verified.")
        entries(current)
        return current

    async def reconcile(
        self, connection: Connection, ident: str, data: dict[str, Any], *, just_sent: bool = False
    ) -> dict[str, Any]:
        progress = data["import_publish"]
        pending = progress["pending"]
        if pending["kind"] == "completion_replace":
            return await self.replaced(connection, ident, data, resend=not just_sent)
        if not data.get("created_id"):
            listing = await self.service.client.call(connection, "getPlaylists")
            matches = [
                p
                for p in listing.get("playlists", {}).get("playlist", [])
                if p.get("owner") == connection.username and p.get("name") == data["provisional"]
            ]
            if len(matches) != 1:
                raise PlaylistError(
                    "Playlist creation has an uncertain result. Recheck after Navidrome recovers; "
                    "no second playlist will be created."
                )
            data["created_id"] = matches[0]["id"]
        current = await self.current(connection, data)
        actual = entries(current)
        if pending["kind"].startswith("completion_"):
            after = staged_now(data, pending["appended"], pending["removed"])
            if actual != after or not same_place(current, pending["metadata"]):
                raise PlaylistError(
                    "The playlist changed or its update is unconfirmed. No mutation was "
                    "repeated. Retry later or stop; existing entries are kept."
                )
            progress.update(
                appended=pending["appended"],
                removed=pending["removed"],
                baseline=fingerprint(current),
                metadata=metadata(current),
            )
            progress.pop("pending")
            await self.save(ident, data)
            return current
        after = data["native_ids"][: pending["end"]]
        expected_meta = pending.get("metadata")
        if expected_meta:
            expected_meta = {**expected_meta, "id": data["created_id"]}
        if actual != after or (expected_meta and metadata(current) != expected_meta):
            if actual == data["native_ids"][: pending["start"]] and not just_sent:
                raise PlaylistError(
                    "The pending append is not confirmed. No append was repeated. Recheck later "
                    "or stop this import; its existing native entries are kept."
                )
            raise PlaylistError(
                "The native playlist changed or its append is unconfirmed. Recheck this import."
            )
        if not expected_meta:
            names = {data["provisional"]}
            if pending.get("legacy"):
                # The former single-write publisher saved created_id before its final
                # rename. A crash after that rename must still reconcile on upgrade.
                names.add(data["definition"]["name"])
            if (
                current.get("name") not in names
                or current.get("owner") != connection.username
                or current.get("public", False)
                or current.get("comment")
            ):
                raise PlaylistError(
                    "The native playlist was edited during creation. Its entries are kept."
                )
        progress.update(
            appended=pending["end"], baseline=fingerprint(current), metadata=metadata(current)
        )
        progress.pop("pending")
        await self.save(ident, data)
        return current

    async def complete(
        self, connection: Connection, mix: dict[str, Any], ident: str, data: dict[str, Any]
    ) -> None:
        """Write the reviewed merge to the same playlist, verify it, then finish."""
        completion, progress = data["completion"], data["import_publish"]
        data["created_id"] = completion["playlist_id"]
        if "playlist_ids" not in data:
            data["playlist_ids"], data["entry_rows"] = materialize(
                completion["plan"], data["rows"], data["native_ids"]
            )
            await self.save(ident, data)
        if not progress.get("written"):
            async with self.service.changes:
                await self.guard(connection, mix, ident)
                if completion["staged"]:
                    await self.stage(connection, ident, data)
                else:
                    await self.replace(connection, ident, data)
            return
        selected = [Track.model_validate(s) for s in data["selected"]]
        if await self.verify(connection, mix, ident, data, selected, data["native_ids"]):
            return
        async with self.service.changes:
            await self.guard(connection, mix, ident)
            current = await self.current(connection, data)
            actual = entries(current)
            kept = [i for i, slot in zip(actual, completion["plan"], strict=False) if "id" in slot]
            if (
                actual != data["playlist_ids"]
                or not same_place(current, completion["metadata"])
                or kept != [s["id"] for s in completion["snapshot"]]
            ):
                raise PlaylistError(DIFFERS)
            rows = data["rows"]
            for number, entry in zip(data["entry_rows"], current["entry"], strict=True):
                if number is not None and not same_recording(
                    rows[number - 1]["track"], native_track(entry)
                ):
                    raise PlaylistError(
                        "The updated playlist differs from its reviewed recordings."
                    )
            await self.finished(connection, mix, ident, data, current, selected)

    async def replace(self, connection: Connection, ident: str, data: dict[str, Any]) -> None:
        """Replace the tracks in one native write; Navidrome keeps the playlist's details."""
        completion, progress = data["completion"], data["import_publish"]
        current = await self.current(connection, data)
        if fingerprint(current) != completion["baseline"]:
            raise PlaylistError(CHANGED)
        progress.update(write="replace", baseline=completion["baseline"])
        if entries(current) == data["playlist_ids"]:
            progress["written"] = True
            await self.save(ident, data)
            return
        data["intent"] = "complete"
        progress["pending"] = {"kind": "completion_replace"}
        await self.save(ident, data)  # Intent BEFORE the native mutation.
        await self.send(connection, ident, data)

    async def send(self, connection: Connection, ident: str, data: dict[str, Any]) -> None:
        # A refusal leaves the playlist unchanged; a lost answer may have applied.
        # Either way the readback decides.
        with contextlib.suppress(PlaylistError):
            await self.service.client.call(
                connection,
                "createPlaylist",
                [
                    ("playlistId", data["created_id"]),
                    *(("songId", i) for i in data["playlist_ids"]),
                ],
            )
        await self.replaced(connection, ident, data, resend=False)

    async def replaced(
        self, connection: Connection, ident: str, data: dict[str, Any], *, resend: bool
    ) -> dict[str, Any]:
        """Observe the one-step write: applied, not applied (safe to send again) or neither."""
        completion, progress = data["completion"], data["import_publish"]
        current = await self.current(connection, data)
        if not same_place(current, completion["metadata"]):
            raise PlaylistError(DIFFERS)
        if entries(current) == data["playlist_ids"]:
            progress.pop("pending", None)
            progress.update(written=True, baseline=fingerprint(current))
            await self.save(ident, data)
            return current
        if entries(current) != [s["id"] for s in completion["snapshot"]]:
            raise PlaylistError(DIFFERS)
        if not resend:
            raise PlaylistError(
                "Navidrome did not update the playlist; it is unchanged. "
                "Retry to send the reviewed update again."
            )
        # Still exactly the reviewed playlist: the same full replacement is idempotent.
        await self.send(connection, ident, data)
        return await self.current(connection, data)

    async def stage(self, connection: Connection, ident: str, data: dict[str, Any]) -> None:
        """Large lists: append the new suffix in batches, then remove the old copies.

        The unchanged prefix stays in place. Stop can leave extra copies, never
        fewer songs; Resume finishes the reviewed update.
        """
        completion, progress = data["completion"], data["import_publish"]
        original = [s["id"] for s in completion["snapshot"]]
        target = data["playlist_ids"]
        if "prefix" not in progress:
            current = await self.current(connection, data)
            if fingerprint(current) != completion["baseline"]:
                raise PlaylistError(CHANGED)
            prefix = next(
                (
                    i
                    for i, (old, new) in enumerate(zip(original, target, strict=False))
                    if old != new
                ),
                min(len(original), len(target)),
            )
            progress.update(
                write="staged",
                prefix=prefix,
                appended=prefix,
                removed=0,
                baseline=fingerprint(current),
                metadata=metadata(current),
            )
        prefix, appended, removed = progress["prefix"], progress["appended"], progress["removed"]
        if appended == len(target) and removed == len(original) - prefix:
            progress["written"] = True
            await self.save(ident, data)
            return
        current = await self.current(connection, data)
        if not same_place(current, progress["metadata"]) or entries(current) != staged_now(
            data, appended, removed
        ):
            raise PlaylistError(
                "Your playlist's songs changed during the update. Its edits are kept; "
                "nothing more was written."
            )
        # A new name, comment or visibility is the owner's and stays.
        progress.update(baseline=fingerprint(current), metadata=metadata(current))
        params = [("playlistId", data["created_id"])]
        if appended < len(target):
            end = min(appended + APPEND_BATCH, len(target))
            params.extend(("songIdToAdd", i) for i in target[appended:end])
            kind, appended = "completion_append", end
        else:
            count = min(APPEND_BATCH, len(original) - prefix - removed)
            params.extend(("songIndexToRemove", str(i)) for i in range(prefix, prefix + count))
            kind, removed = "completion_remove", removed + count
        data["intent"] = "complete"
        progress["pending"] = {
            "kind": kind,
            "appended": appended,
            "removed": removed,
            "metadata": metadata(current),
        }
        await self.save(ident, data)
        await self.service.client.call(connection, "updatePlaylist", params)
        await self.reconcile(connection, ident, data, just_sent=True)

    async def verify(
        self,
        connection: Connection,
        mix: dict[str, Any],
        ident: str,
        data: dict[str, Any],
        selected: list[Track],
        ids: list[str],
    ) -> bool:
        """Verify frozen recording identities in one bounded group; False when done."""
        progress = data["import_publish"]
        start = progress["verified"]
        if start >= len(selected):
            return False
        verification_batch = list(zip(selected, ids, strict=True))[start : start + PREPARE_BATCH]
        checked: set[str] = set()
        for song, native_id in verification_batch:
            key = song.model_dump_json() + native_id
            if key in checked:
                continue
            await self.guard(connection, mix, ident)
            native = await self.service.native_track(connection, native_id, library_bound=True)
            if not same_recording(song.model_dump(), native):
                raise PlaylistError("A saved native track is no longer the reviewed recording.")
            checked.add(key)
        progress["verified"] = start + len(verification_batch)
        await self.save(ident, data)
        return True

    async def finished(
        self,
        connection: Connection,
        mix: dict[str, Any],
        ident: str,
        data: dict[str, Any],
        current: dict[str, Any],
        selected: list[Track],
    ) -> None:
        principal = None
        if self.service.published is not None or self.service.imported is not None:
            await self.service.client.validate(connection)
            principal = await self.service.client.principal(connection)
        async with self.imports.store.transaction() as db:
            if principal is not None and self.service.published is not None:
                # Only import entries: the owner's own songs are not attributed here.
                await self.service.published(principal, selected, data["native_ids"], db)
            if principal is not None and self.service.imported is not None:
                # The import's songs bring their albums into the account's Original
                # imported library.
                await self.service.imported(principal, mix["id"], data["native_ids"], db)
            await db.execute(
                "UPDATE playlist_mixes SET playlist_id=?,published=?,due=?,enabled=0 WHERE id=?",
                [current["id"], fingerprint(current), time.time(), mix["id"]],
            )
            await db.execute(
                "UPDATE playlist_runs SET state='published',data=?,error=''"
                " WHERE id=? AND state='publishing'",
                [json.dumps(data), ident],
            )

    async def step(
        self, connection: Connection, mix: dict[str, Any], ident: str, data: dict[str, Any]
    ) -> None:
        await self.guard(connection, mix, ident)
        definition = Definition.model_validate(data["definition"])
        if definition != Definition.model_validate_json(mix["definition"]):
            raise PlaylistError("This import uses previous rules. Review it again.")
        await self.imports.reviewed(mix, data)
        selected = [Track.model_validate(s) for s in data["selected"]]
        validate(selected, definition)
        legacy = "import_publish" not in data
        progress = data.setdefault("import_publish", {"appended": 0, "verified": 0})
        ids = data.setdefault("native_ids", [])
        if data.get("completion") and "write" not in progress:
            current = await self.service.client.playlist(
                connection, data["completion"]["playlist_id"]
            )
            if fingerprint(current) != data["completion"]["baseline"]:
                raise PlaylistError(CHANGED)
        # Pre-existing reviewed import publications used a single native replacement.
        # Their original intent can be reconciled, never replaced with a new creation.
        if data.get("intent") and not progress.get("baseline") and not progress.get("pending"):
            progress["pending"] = {"kind": "create", "start": 0, "end": len(ids), "legacy": legacy}
        if len(ids) < len(selected):
            batch = selected[len(ids) : len(ids) + PREPARE_BATCH]
            approved = {r["ref"]: r["fingerprint"] for r in data.get("releases", [])}
            # Slow catalogs (an add-on's) can take long for each release: the batch ends at
            # the first song whose release was not checked in time, so every turn prepares
            # and saves something and the next one goes on from there.
            deadline = time.monotonic() + self.service.settings.run_seconds / 2
            checked: set[str] = set()
            for position, song in enumerate(batch):
                ref = song.release
                if not ref or ref in checked:
                    continue
                if position and time.monotonic() > deadline:
                    batch = batch[:position]
                    break
                checked.add(ref)
                # The catalog's answer now, past its cache: also an add-on's catalog.
                fresh = await self.service.catalog_of(CatalogRef.parse(ref).catalog, addons=True)
                if fresh is None:
                    raise PlaylistError(
                        "The catalog of a reviewed release is unavailable. Retry later."
                    )
                # Past the cache, whose entry it then refreshes: preparing the songs
                # reads the same answer instead of asking a slow catalog again.
                if isinstance(fresh, CachedCatalog):
                    album = await fresh.fresh_album(CatalogRef.parse(ref).id)
                else:
                    album = await fresh.album(CatalogRef.parse(ref).id)
                if release_fingerprint(album) != approved.get(ref):
                    raise PlaylistError(
                        "A reviewed release changed. Start a new import before adding it."
                    )
            unique = {s.model_dump_json(): s for s in batch}
            prepared = await self.service.check_tracks(
                connection,
                list(unique.values()),
                approved,
                library_bound=True,
                authorize=lambda: self.guard(connection, mix, ident),
                addons=True,
            )
            mapping = dict(zip(unique, prepared, strict=True))
            ids.extend(mapping[s.model_dump_json()] for s in batch)
            await self.save(ident, data)
            return
        if progress.get("pending"):
            async with self.service.changes:
                await self.guard(connection, mix, ident)
                await self.reconcile(connection, ident, data)
            return
        if data.get("completion"):
            await self.complete(connection, mix, ident, data)
            return
        if progress["appended"] < len(ids):
            async with self.service.changes:
                await self.guard(connection, mix, ident)
                start = progress["appended"]
                end = min(start + APPEND_BATCH, len(ids))
                if data.get("created_id"):
                    current = await self.current(connection, data)
                    if (
                        fingerprint(current) != progress["baseline"]
                        or entries(current) != ids[:start]
                    ):
                        raise PlaylistError(
                            "The native playlist was edited outside this import. "
                            "Its entries are kept."
                        )
                    progress["pending"] = {
                        "kind": "append",
                        "start": start,
                        "end": end,
                        "metadata": metadata(current),
                        "before": fingerprint(current),
                    }
                    method = "updatePlaylist"
                    params = [
                        ("playlistId", data["created_id"]),
                        *(("songIdToAdd", i) for i in ids[start:end]),
                    ]
                else:
                    if start:
                        raise PlaylistError("The saved native playlist identity is unavailable.")
                    data["provisional"] = f"Shijhon preparing {secrets.token_hex(16)}"
                    data["intent"] = "create"
                    progress["pending"] = {"kind": "create", "start": 0, "end": end}
                    method = "createPlaylist"
                    params = [("name", data["provisional"]), *(("songId", i) for i in ids[:end])]
                await self.save(ident, data)  # Intent BEFORE any native mutation.
                reply = await self.service.client.call(connection, method, params)
                if method == "createPlaylist":
                    data["created_id"] = reply["playlist"]["id"]
                    progress["pending"]["metadata"] = metadata(reply["playlist"])
                    await self.save(ident, data)
                await self.reconcile(connection, ident, data, just_sent=True)
            return
        # Verify frozen recording identities in bounded groups as well as the full
        # ordered prefix. Repeats remain repeated in the native playlist and evidence.
        if await self.verify(connection, mix, ident, data, selected, ids):
            return
        async with self.service.changes:
            await self.guard(connection, mix, ident)
            current = await self.current(connection, data)
            if entries(current) != ids:
                raise PlaylistError("The native playlist changed before import completion.")
            if progress.get("rename_pending"):
                expected_meta = {**progress["metadata"], "name": definition.name}
                if metadata(current) == expected_meta:
                    progress.update(baseline=fingerprint(current), metadata=metadata(current))
                    progress.pop("rename_pending")
                elif fingerprint(current) != progress["baseline"]:
                    raise PlaylistError(
                        "The native playlist was edited during its rename. Its entries are kept."
                    )
            if fingerprint(current) != progress["baseline"]:
                raise PlaylistError(
                    "The native playlist was edited before completion. Its entries are kept."
                )
            if current["name"] != definition.name:
                progress["rename_pending"] = True
                await self.save(ident, data)
                await self.service.client.call(
                    connection,
                    "updatePlaylist",
                    [
                        ("playlistId", current["id"]),
                        ("name", definition.name),
                    ],
                )
                current = await self.current(connection, data)
                if entries(current) != ids or metadata(current) != {
                    **progress["metadata"],
                    "name": definition.name,
                }:
                    raise PlaylistError("The native playlist changed during its final rename.")
                progress.update(baseline=fingerprint(current), metadata=metadata(current))
                progress.pop("rename_pending", None)
                await self.save(ident, data)
            for wanted, entry in zip(selected, current.get("entry", []), strict=True):
                if not same_recording(wanted.model_dump(), native_track(entry)):
                    raise PlaylistError(
                        "The final native playlist differs from its reviewed recordings."
                    )
            await self.finished(connection, mix, ident, data, current, selected)
