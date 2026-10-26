# ruff: noqa: S608 -- SQL identifiers are constants or validated; values are bound.
"""Bounded caller-authenticated scans with an atomic last-complete snapshot."""

from __future__ import annotations

import json
import math
import secrets
import time
from collections.abc import Mapping, Sequence
from typing import Any

import anyio
import anyio.lowlevel

from shijhon.collection.contracts import Access, AssetLookup, CollectionError
from shijhon.store import Store

COLUMNS = (
    "library_id,track_key,song_id,release_key,album_id,artist_id,title,album,artist,"
    "search_text,duration,size,year,metadata,original,present,generation,indexed_at"
)


class Index:
    def __init__(
        self,
        store: Store,
        access: Access,
        managed_folder: str,
        *,
        asset_lookup: AssetLookup | None = None,
    ) -> None:
        self.store, self.access, self.managed_folder = store, access, managed_folder.strip("/")
        self.asset_lookup = asset_lookup
        self.lock = anyio.Lock()

    async def scan(self, owner_id: str, library_id: str) -> None:
        async with self.lock:
            await self._authorize(owner_id, library_id)
            generation = secrets.token_hex(16)
            await self.store.execute(
                "INSERT INTO collection_index(library_id,state) VALUES (?,'indexing')"
                " ON CONFLICT(library_id) DO UPDATE SET state='indexing',error=''",
                [library_id],
            )
            offset = 0
            try:
                while True:
                    # Each source page authenticates as the original caller. Current
                    # immutable identity/grants are also checked at scan boundaries,
                    # including immediately before the atomic publish below.
                    songs = await self.access.songs(owner_id, library_id, offset, 500)
                    if len(songs) > 500:
                        raise CollectionError("The library index returned an oversized page.")
                    if not songs:
                        break
                    duplicate = await self.store.fetchone(
                        "SELECT 1 FROM collection_scan_tracks WHERE library_id=?"
                        " AND generation=? AND song_id IN (SELECT value FROM json_each(?))",
                        [library_id, generation, json.dumps([str(s.get("id", "")) for s in songs])],
                    )
                    if duplicate:
                        raise CollectionError(
                            "The native library repeated a song during indexing. Retry the refresh."
                        )
                    rows, aliases = await self._rows(library_id, generation, songs)
                    async with self.store.transaction() as conn:
                        await conn.executemany(
                            f"INSERT INTO collection_scan_tracks({COLUMNS})"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            rows,
                        )
                        await conn.executemany(
                            "INSERT INTO collection_scan_aliases VALUES (?,?,?,?)",
                            aliases,
                        )
                        await conn.execute(
                            "UPDATE collection_index SET indexed=? WHERE library_id=?",
                            [offset + len(songs), library_id],
                        )
                    offset += len(songs)
                    if len(songs) < 500:
                        break
                    await anyio.lowlevel.checkpoint()
                await self._authorize(owner_id, library_id)
                async with self.store.transaction() as conn:
                    # Publication must never silently repoint a durable alias,
                    # including when import publication raced this structural scan.
                    cursor = await conn.execute(
                        "SELECT 1 FROM collection_scan_aliases s"
                        " JOIN collection_track_aliases a ON a.library_id=s.library_id"
                        " AND a.alias=s.alias WHERE s.library_id=? AND s.generation=?"
                        " AND a.track_key<>s.track_key LIMIT 1",
                        [library_id, generation],
                    )
                    if await cursor.fetchone():
                        raise CollectionError(
                            "The music index contains conflicting durable identities."
                        )
                    # A refresh with identical structural facts keeps its generation:
                    # native query caches need no rebuild merely because time passed.
                    comparable = ",".join(
                        c for c in COLUMNS.split(",") if c not in ("generation", "indexed_at")
                    )
                    different = await conn.execute(
                        "SELECT 1 FROM ("
                        f"SELECT {comparable} FROM collection_scan_tracks"
                        " WHERE library_id=? AND generation=? EXCEPT "
                        f"SELECT {comparable} FROM collection_tracks"
                        " WHERE library_id=? AND present=1) LIMIT 1",
                        [library_id, generation, library_id],
                    )
                    unchanged = await different.fetchone() is None
                    reverse = await conn.execute(
                        "SELECT 1 FROM ("
                        f"SELECT {comparable} FROM collection_tracks"
                        " WHERE library_id=? AND present=1 EXCEPT "
                        f"SELECT {comparable} FROM collection_scan_tracks"
                        " WHERE library_id=? AND generation=?) LIMIT 1",
                        [library_id, library_id, generation],
                    )
                    unchanged = unchanged and await reverse.fetchone() is None
                    current = await conn.execute(
                        "SELECT generation FROM collection_index WHERE library_id=?",
                        [library_id],
                    )
                    previous = await current.fetchone()
                    if unchanged and previous and previous["generation"]:
                        await conn.execute(
                            "INSERT INTO collection_track_aliases SELECT library_id,alias,track_key"
                            " FROM collection_scan_aliases WHERE library_id=? AND generation=?"
                            " ON CONFLICT(library_id,alias) DO NOTHING",
                            [library_id, generation],
                        )
                        await conn.execute(
                            "UPDATE collection_index SET state='ready',indexed=?,updated_at=?,"
                            "error='' WHERE library_id=?",
                            [offset, time.time(), library_id],
                        )
                        return
                    await conn.execute(
                        "UPDATE collection_tracks SET present=0 WHERE library_id=?", [library_id]
                    )
                    await conn.execute(
                        f"INSERT INTO collection_tracks({COLUMNS}) SELECT {COLUMNS}"
                        " FROM collection_scan_tracks WHERE library_id=? AND generation=?"
                        " ON CONFLICT(library_id,track_key) DO UPDATE SET "
                        + ",".join(
                            f"{c}=excluded.{c}"
                            for c in COLUMNS.split(",")
                            if c not in ("library_id", "track_key")
                        ),
                        [library_id, generation],
                    )
                    await conn.execute(
                        "INSERT INTO collection_track_aliases SELECT library_id,alias,track_key"
                        " FROM collection_scan_aliases WHERE library_id=? AND generation=?"
                        " ON CONFLICT(library_id,alias) DO NOTHING",
                        [library_id, generation],
                    )
                    # The new snapshot holds them: the in-place additions to the last go.
                    await conn.execute(
                        "DELETE FROM collection_index_additions WHERE library_id=?"
                        " AND generation<>?",
                        [library_id, generation],
                    )
                    await conn.execute(
                        "UPDATE collection_index SET generation=?,state='ready',indexed=?,"
                        "updated_at=?,error='' WHERE library_id=?",
                        [generation, offset, time.time(), library_id],
                    )
            except BaseException:
                with anyio.CancelScope(shield=True):
                    await self.store.execute(
                        "UPDATE collection_index SET state='failed',"
                        "error='Index refresh stopped. The last complete index is retained.'"
                        " WHERE library_id=?",
                        [library_id],
                    )
                raise
            finally:
                with anyio.CancelScope(shield=True):
                    await self.store.execute(
                        "DELETE FROM collection_scan_aliases WHERE library_id=?",
                        [library_id],
                    )
                    await self.store.execute(
                        "DELETE FROM collection_scan_tracks WHERE library_id=?", [library_id]
                    )

    async def add(
        self, library_id: str, songs: Sequence[Mapping[str, Any]]
    ) -> tuple[str, list[str]]:
        """Placeholders Shijhon wrote after the last complete snapshot (``written.py``), as
        a verified caller read them, added to it in one transaction, in its generation,
        and recorded as one addition (``collection_index_additions``): the query index's
        cached facts take every recorded addition in before they are read
        (``QueryIndex``), so its lists stay valid. Only songs whose placeholder - of a
        release not being taken out - is still recorded when the transaction writes are
        added, as Shijhon's: a song whose record went is skipped, never taken for an
        original. Songs the snapshot has keep their rows; none is added without a
        snapshot. Returns the generation and the songs added."""
        if len(songs) > 500:
            raise CollectionError("Add at most 500 songs to the library index at once.")
        async with self.lock:
            state = await self.store.fetchone(
                "SELECT generation FROM collection_index WHERE library_id=?", [library_id]
            )
            if not state or not state["generation"] or not songs:
                return "", []
            generation = str(state["generation"])
            ids = [str(s.get("id", "")) for s in songs]
            indexed = {
                str(r["song_id"])
                for r in await self.store.fetchall(
                    "SELECT song_id FROM collection_tracks WHERE library_id=? AND present=1"
                    " AND song_id IN (SELECT value FROM json_each(?))",
                    [library_id, json.dumps(ids)],
                )
            }
            new = [s for s in songs if str(s.get("id", "")) not in indexed]
            if not new:
                return generation, []
            rows, aliases = await self._rows(library_id, generation, new)
            async with self.store.transaction() as conn:
                cursor = await conn.execute(
                    "SELECT generation FROM collection_index WHERE library_id=?", [library_id]
                )
                current = await cursor.fetchone()
                if not current or current["generation"] != generation:
                    return generation, []  # a full refresh came first: it has them
                cursor = await conn.execute(
                    "SELECT p.song_id,p.release_ref FROM placeholders p JOIN releases r"
                    " ON r.ref=p.release_ref WHERE r.removing_at IS NULL"
                    " AND p.song_id IN (SELECT value FROM json_each(?))",
                    [json.dumps([str(row[2]) for row in rows])],
                )
                recorded = {
                    (str(r["song_id"]), str(r["release_ref"])) for r in await cursor.fetchall()
                }
                # Shijhon's own placeholders, as recorded now: anything else is not added.
                rows = [r for r in rows if not r[14] and (str(r[2]), str(r[3])) in recorded]
                keys = {str(r[1]) for r in rows}
                aliases = [a for a in aliases if a[3] in keys]
                if not rows:
                    return generation, []
                for library, _, alias, key in aliases:
                    cursor = await conn.execute(
                        "SELECT track_key FROM collection_track_aliases"
                        " WHERE library_id=? AND alias=?",
                        [library, alias],
                    )
                    old = await cursor.fetchone()
                    if old and old["track_key"] != key:
                        raise CollectionError(
                            "The music index contains conflicting durable identities."
                        )
                await conn.executemany(
                    f"INSERT INTO collection_tracks({COLUMNS})"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(library_id,track_key) DO UPDATE SET "
                    + ",".join(
                        f"{c}=excluded.{c}"
                        for c in COLUMNS.split(",")
                        if c not in ("library_id", "track_key")
                    ),
                    rows,
                )
                await conn.executemany(
                    "INSERT OR IGNORE INTO collection_track_aliases VALUES (?,?,?)",
                    [(library, alias, key) for library, _, alias, key in aliases],
                )
                await conn.execute(
                    "INSERT INTO collection_index_additions SELECT ?,?,COALESCE((SELECT"
                    " max(revision) FROM collection_index_additions WHERE library_id=?"
                    " AND generation=?),0)+1,value FROM json_each(?)",
                    [
                        library_id,
                        generation,
                        library_id,
                        generation,
                        json.dumps([str(row[2]) for row in rows]),
                    ],
                )
                await conn.execute(
                    "UPDATE collection_index SET updated_at=?,indexed=(SELECT count(*)"
                    " FROM collection_tracks WHERE library_id=? AND present=1)"
                    " WHERE library_id=?",
                    [time.time(), library_id, library_id],
                )
            return generation, [str(row[2]) for row in rows]

    async def _authorize(self, owner: str, library: str) -> None:
        actor = await self.access.authorize(owner)
        if actor.owner_id != owner or library not in actor.library_ids:
            raise CollectionError("Your access to this music library could not be verified.")

    async def _rows(
        self, library: str, generation: str, songs: Any
    ) -> tuple[list[tuple[Any, ...]], list[tuple[str, str, str, str]]]:
        ids = [str(s.get("id", "")) for s in songs]
        if any(not s or len(s) > 300 for s in ids) or len(set(ids)) != len(ids):
            raise CollectionError("The native library returned invalid song identities.")
        assets = await self.asset_lookup(library, songs) if self.asset_lookup else {}
        if any(
            ident not in ids or not asset or len(asset) > 300 for ident, asset in assets.items()
        ):
            raise CollectionError("The imported asset lookup returned invalid identities.")
        marks = ",".join("?" for _ in ids)
        managed = {
            r["song_id"]: dict(r)
            for r in await self.store.fetchall(
                "SELECT song_id,track_ref,release_ref FROM placeholders"
                f" WHERE song_id IN ({marks})",
                ids,
            )
        }
        owned = {
            r["song_id"]
            for r in await self.store.fetchall(
                f"SELECT song_id FROM track_links WHERE owned=1 AND song_id IN ({marks})",
                ids,
            )
        }
        aliases = (
            ["native:" + ident for ident in ids]
            + ["catalog:" + str(link["track_ref"]) for link in managed.values()]
            + ["import:" + asset for asset in assets.values()]
        )
        known = {
            r["alias"]: r["track_key"]
            for r in await self.store.fetchall(
                "SELECT alias,track_key FROM collection_track_aliases WHERE library_id=?"
                " AND alias IN (SELECT value FROM json_each(?))",
                [library, json.dumps(aliases)],
            )
        }
        result, staged_aliases = [], []
        for song in songs:
            row = list(self._row(library, generation, song, managed, owned))
            native_alias = "native:" + str(song["id"])
            link = managed.get(str(song["id"]))
            catalog_alias = "catalog:" + str(link["track_ref"]) if link else ""
            asset_alias = "import:" + assets[str(song["id"])] if str(song["id"]) in assets else ""
            keys = {
                known[alias]
                for alias in (native_alias, catalog_alias, asset_alias)
                if alias in known
            }
            if len(keys) > 1:
                raise CollectionError("The music index contains conflicting durable identities.")
            row[1] = next(iter(keys)) if keys else row[1]
            result.append(tuple(row))
            for alias in (native_alias, catalog_alias, asset_alias):
                if alias:
                    staged_aliases.append((library, generation, alias, str(row[1])))
        return result, staged_aliases

    def _row(
        self,
        library: str,
        generation: str,
        song: Mapping[str, Any],
        managed: dict[str, dict[str, Any]],
        owned: set[str],
    ) -> tuple[Any, ...]:
        ident = str(song["id"])
        link = managed.get(ident)
        path = str(song.get("path", "")).replace("\\", "/").strip("/")
        in_managed = bool(
            self.managed_folder
            and (
                path.startswith(self.managed_folder + "/")
                or ("/" + self.managed_folder + "/") in ("/" + path)
            )
        )
        original = ident in owned or not (link or in_managed)
        key = (
            "native:" + ident
            if original
            else "catalog:" + (str(link["track_ref"]) if link else ident)
        )
        album_id = str(song.get("albumId", ""))
        artist_id = str(song.get("artistId") or "name:" + str(song.get("artist", "")))
        title, album, artist = (str(song.get(k, ""))[:1000] for k in ("title", "album", "artist"))
        release = str(link["release_ref"]) if link else "native:" + album_id
        return (
            library,
            key,
            ident,
            release,
            album_id,
            artist_id,
            title,
            album,
            artist,
            " ".join((title, album, artist)).casefold(),
            number(song, "duration"),
            int(number(song, "size")),
            int(number(song, "year")),
            structural_metadata(song),
            int(original),
            1,
            generation,
            time.time(),
        )


def number(song: Mapping[str, Any], key: str) -> float:
    value = song.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CollectionError("The native library returned invalid numeric metadata.")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1e15:
        raise CollectionError("The native library returned invalid numeric metadata.")
    return value


def structural_metadata(song: Mapping[str, Any]) -> str:
    # This cache is shared by library. Never copy caller annotations or unknown fields.
    # Navidrome's native song fields (``model/mediafile.go``): what tells recordings and
    # albums apart for music imports (``music_imports/overlap.py``) - the MusicBrainz
    # recording and album IDs, the explicit status ("e", "c" or ""), positions, the
    # compilation flag, whether the file has its own picture - and its audio quality.
    fields = (
        "id",
        "albumId",
        "artistId",
        "albumArtistId",
        "title",
        "album",
        "artist",
        "albumArtist",
        "displayArtist",
        "displayAlbumArtist",
        "genre",
        "isrc",
        "musicBrainzId",
        "musicBrainzAlbumId",
        "musicBrainzTrackId",
        "mbzRecordingID",
        "mbzAlbumId",
        "mbzReleaseTrackId",
        "explicitStatus",
        "releaseDate",
        "suffix",
        "contentType",
        "coverArt",
    )
    data: dict[str, Any] = {}
    for key in fields:
        value = song.get(key)
        if isinstance(value, str):
            data[key] = value[:2000]
    for key in ("duration", "size", "year", "track", "discNumber", "bitRate"):
        if key in song:
            data[key] = number(song, key)
    for key in ("trackNumber", "sampleRate", "bitDepth"):
        if song.get(key) is not None:
            data[key] = number(song, key)
    for key in ("compilation", "hasCoverArt"):
        if isinstance(song.get(key), bool):
            data[key] = song[key]
    for key in ("genres", "artists", "albumArtists", "participants"):
        values = song.get(key)
        if not isinstance(values, list):
            continue
        if len(values) > 100:
            raise CollectionError("The native library returned too many metadata participants.")
        data[key] = [
            {
                k: v[:2000]
                for k, v in item.items()
                if k in ("id", "name", "role", "subRole", "musicBrainzId") and isinstance(v, str)
            }
            for item in values
            if isinstance(item, dict)
        ]
    for key, allowed in (("participants", ("id", "name", "subRole")), ("tags", ("id", "value"))):
        mapping = song.get(key)
        if not isinstance(mapping, dict):
            continue
        if len(mapping) > 100:
            raise CollectionError("The native library returned oversized structural metadata.")
        clean: dict[str, Any] = {}
        for category, values in mapping.items():
            if not isinstance(category, str) or len(category) > 80 or not category.isprintable():
                continue
            if not isinstance(values, list) or len(values) > 100:
                raise CollectionError("The native library returned invalid structural metadata.")
            clean[category] = [
                item[:2000]
                if isinstance(item, str)
                else {k: v[:2000] for k, v in item.items() if k in allowed and isinstance(v, str)}
                for item in values
                if isinstance(item, dict) or (key == "tags" and isinstance(item, str))
            ]
        data[key] = clean
    result = json.dumps(data, allow_nan=False, separators=(",", ":"))
    if len(result.encode()) > 32768:
        raise CollectionError("The native library returned oversized song metadata.")
    return result
