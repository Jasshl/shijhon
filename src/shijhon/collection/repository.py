# ruff: noqa: S608 -- SQL fragments are fixed choices; all user values are bound.
"""SQL-backed bounded browsing and immutable bulk selection snapshots."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from shijhon.collection.contracts import Query
from shijhon.collection.policy import (
    LINK_GLOB,
    builtin_profile_id,
    builtin_selection,
    chosen_albums,
    original_albums,
    own_sql,
    shijhon_albums,
)
from shijhon.store import Store
from shijhon.views.ids import CatalogId

# Every statement that reads the rules starts with ``WITH_KEYS``: the owner, the library,
# the owner's Original and Shijhon profiles and the profile browsed or changed, bound once,
# which the rules' subqueries read (so each subquery is read once per statement, and
# repeating a rule repeats no parameter).
WITH_KEYS = (
    "WITH k AS (SELECT ? AS owner_id,? AS library_id,? AS original_id,? AS shijhon_id,"
    "? AS profile_id) "
)
_OWNER, _LIBRARY = "(SELECT owner_id FROM k)", "(SELECT library_id FROM k)"
# The albums each built-in library holds by its rule (``policy.original_albums``,
# ``policy.shijhon_albums``).
IN_ORIGINAL = "t.album_id IN (" + original_albums("", "", keys=(_OWNER, _LIBRARY)).sql + ")"
IN_SHIJHON = "t.album_id IN (" + shijhon_albums("", "", keys=(_OWNER, _LIBRARY)).sql + ")"
# The albums added by hand to All, and to the profile browsed or changed.
IN_ALL_CHOSEN = "t.album_id IN (" + chosen_albums("", "", "all", keys=(_OWNER, _LIBRARY)).sql + ")"
IN_CHOSEN = (
    "t.album_id IN ("
    + chosen_albums("", "", "(SELECT profile_id FROM k)", keys=(_OWNER, _LIBRARY)).sql
    + ")"
)
# The owner's hand choice in Original imported library or Shijhon library: looked up only
# for the songs with a choice in either (a set read once per statement).
_CHOSEN = (
    f"t.track_key IN (SELECT c.track_key FROM collection_membership c WHERE c.owner_id={_OWNER}"
    f" AND c.library_id={_LIBRARY} AND c.profile_id IN ((SELECT original_id FROM k),"
    "(SELECT shijhon_id FROM k)))"
)


def _choice(profile: str) -> str:
    return (
        f"CASE WHEN {_CHOSEN} THEN (SELECT c.saved FROM collection_membership c"
        f" WHERE c.owner_id={_OWNER} AND c.profile_id=(SELECT {profile} FROM k)"
        " AND c.library_id=t.library_id AND c.track_key=t.track_key) END"
    )


# The songs in the owner's history (a set read once per statement).
IN_HISTORY = (
    "t.track_key IN (SELECT h.track_key FROM collection_history h"
    f" WHERE h.owner_id={_OWNER} AND h.library_id={_LIBRARY})"
)
JOIN = (
    " FROM collection_tracks t JOIN collection_profiles p ON "
    "p.owner_id=? AND p.id=? AND p.archived=0"
    " JOIN collection_library_policy lp ON lp.library_id=t.library_id"
    " LEFT JOIN collection_originals o ON o.owner_id=p.owner_id"
    " AND o.library_id=t.library_id AND o.track_key=t.track_key"
    " LEFT JOIN collection_membership m ON m.owner_id=p.owner_id AND m.profile_id=p.id"
    " AND m.library_id=t.library_id AND m.track_key=t.track_key"
)
KEYS = {"song": "track_key", "album": "album_id", "artist": "artist_id"}
# What a listed group counts as a whole (``Repository.browse``).
FULL = ("all_tracks", "all_imported", "full_saved", "full_restorable")
# The owner's own import (personal credit, or a song its import was linked to): the old
# "Your imports"/"Shared imports" source filters of recorded changes.
OWN_SQL = (
    "((t.original=1 AND o.owner_id IS NOT NULL)"
    f" OR coalesce(o.provenance_id,'') GLOB '{LINK_GLOB}')"
)
# Source "Imported": an imported file, or a song the owner's import was linked to. Every
# other song is "From the catalog".
IMPORTED_SQL = f"(t.original=1 OR coalesce(o.provenance_id,'') GLOB '{LINK_GLOB}')"
BASE_SQL = own_sql("t", "o")
# Each library's rule, then its own hand choice (``policy.predicate``).
ORIGINAL_SQL = f"COALESCE({_choice('original_id')},{BASE_SQL} OR {IN_ORIGINAL})"
SHIJHON_SQL = f"COALESCE({_choice('shijhon_id')},{IN_SHIJHON})"
ALL_SQL = f"COALESCE(m.saved,{ORIGINAL_SQL} OR {SHIJHON_SQL} OR {IN_ALL_CHOSEN})"
# A custom library: its hand choices, and the whole albums of the songs added to it.
CUSTOM_SQL = f"COALESCE(m.saved,{IN_CHOSEN})"


def keys(owner: str, library: str, profile_id: str) -> list[Any]:
    """The parameters of ``WITH_KEYS``."""
    return [
        owner,
        library,
        builtin_profile_id(owner, "original"),
        builtin_profile_id(owner, "shijhon"),
        profile_id,
    ]


def joined(owner: str, profile_id: str, library: str) -> list[Any]:
    """The parameters of ``WITH_KEYS`` and ``JOIN``, in this order."""
    return [*keys(owner, library, profile_id), owner, profile_id]


def selection_of(owner: str, profile_id: str, selection: str = "") -> str:
    """The built-in library a profile is ('' for a custom one), also for changes recorded
    before each built-in library had a profile of its own."""
    return selection or builtin_selection(owner, profile_id)


def rule_sql(selection: str) -> str:
    return {"original": ORIGINAL_SQL, "shijhon": SHIJHON_SQL, "all": ALL_SQL}.get(
        selection, CUSTOM_SQL
    )


RESTORABLE_SQL = (
    "(t.present=0 AND t.original=0 AND EXISTS(SELECT 1 FROM collection_history h"
    " JOIN collection_track_aliases a ON a.library_id=h.library_id AND a.track_key=h.track_key"
    " WHERE h.owner_id=p.owner_id AND h.library_id=t.library_id AND h.track_key=t.track_key"
    " AND h.catalog_data<>'{}' AND a.alias='catalog:' ||"
    " json_extract(h.catalog_data,'$.ref.catalog') || ':' ||"
    " json_extract(h.catalog_data,'$.ref.id')))"
)


def pool_sql(query: Query) -> str:
    if query.pool == "server":
        return "t.present=1"
    # The music the account collected: its imports (every original while they are shared),
    # the albums its built-in libraries hold by their rules, what the browsed library
    # holds by hand, and every song in its history - also after it left a library.
    return (
        f"({BASE_SQL} OR (t.present=1 AND ({IN_ORIGINAL} OR {IN_SHIJHON} OR {IN_ALL_CHOSEN}"
        f" OR {IN_CHOSEN} OR COALESCE(m.saved,0)=1)) OR {IN_HISTORY})"
    )


def saved_sql(selection: str = "") -> str:
    return f"(t.present=1 AND ({rule_sql(selection)}))"


def scope_sql(field: str, value: str) -> tuple[str, list[Any]]:
    """Keep a metadata-only drilldown valid after verified native preparation."""
    catalog = CatalogId.parse(value)
    if field == "album_id" and catalog is not None and catalog.kind == "al":
        return "(t.album_id=? OR t.release_key=?)", [value, str(catalog.ref)]
    if field == "artist_id":
        history = (
            "EXISTS(SELECT 1 FROM collection_history h WHERE h.owner_id=p.owner_id"
            " AND h.library_id=t.library_id AND h.track_key=t.track_key AND "
        )
        if value.startswith("name:"):
            return (
                "(t.artist_id=? OR " + history + "json_extract(h.catalog_data,'$.artist')=?))",
                [value, value[5:]],
            )
        if catalog is not None and catalog.kind == "ar":
            return (
                "(t.artist_id=? OR "
                + history
                + "json_extract(h.catalog_data,'$.artist_refs[0].catalog')=?"
                " AND json_extract(h.catalog_data,'$.artist_refs[0].id')=?))",
                [value, catalog.ref.catalog, catalog.ref.id],
            )
    # The caller selects identifiers only from these fixed structural fields.
    assert field in ("album_id", "artist_id")
    return f"t.{field}=?", [value]


def where(library: str, query: Query, selection: str = "") -> tuple[str, list[Any]]:
    sql = "t.library_id=? AND " + pool_sql(query)
    params: list[Any] = [library]
    effective = saved_sql(selection)
    if query.view == "saved":
        sql += f" AND ({effective})=1"
    elif query.view == "unsaved":
        sql += f" AND ({effective})=0"
    elif query.view == "original":
        # Recorded changes keep the physical-source meaning of their old filter values.
        sql += " AND t.original=1"
    if query.origin == "imported":
        sql += " AND " + IMPORTED_SQL
    elif query.origin == "catalog":
        sql += " AND NOT " + IMPORTED_SQL
    elif query.origin == "original":
        sql += " AND t.original=1"
    elif query.origin == "own":
        sql += " AND " + OWN_SQL
    elif query.origin == "shared":
        sql += " AND t.original=1 AND NOT " + OWN_SQL
    if query.search.strip():
        sql += " AND instr(t.search_text,?)>0"
        params.append(query.search.strip().casefold())
    for field in ("album_id", "artist_id"):
        if value := getattr(query, field):
            scoped, values = scope_sql(field, value)
            sql += " AND " + scoped
            params.extend(values)
    return sql, params


class Repository:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def browse(
        self, owner: str, library: str, query: Query, profile_id: str, selection_id: str = ""
    ) -> dict[str, Any]:
        selection = selection_of(owner, profile_id, selection_id)
        condition, params = where(library, query, selection)
        effective = saved_sql(selection)
        pool = pool_sql(query)
        key = KEYS[query.scope]
        title = {"song": "title", "album": "album", "artist": "artist"}[query.scope]
        ordering = {
            "title": "title COLLATE NOCASE, item_key",
            "artist": "artist COLLATE NOCASE,title COLLATE NOCASE,item_key",
            "newest": "newest DESC,item_key",
        }[query.sort]
        # Rows, totals, ownership and full-group badges share one SQLite snapshot.
        join = joined(owner, profile_id, library)
        async with self.store.transaction() as conn:
            # The page's groups count all matching groups (``group_total``); only a page
            # past the end counts them on its own.
            cursor = await conn.execute(
                WITH_KEYS
                + f"SELECT t.{key} AS item_key,min(t.{title}) AS title,min(t.artist) AS artist,"
                "min(t.album) AS album,min(t.album_id) AS album_id,min(t.artist_id) AS artist_id,"
                "count(*) AS tracks,sum(t.original) AS originals,"
                "sum(t.present) AS available_tracks,sum(t.present=0) AS unavailable_tracks,"
                f"sum({RESTORABLE_SQL}) AS restorable_tracks,"
                f"sum({IMPORTED_SQL}) AS imported_tracks,1 AS editable,"
                "max(json_extract(t.metadata,'$.coverArt')) AS cover_art,"
                f"sum({effective}) AS saved,sum(t.duration) AS duration,"
                "max(t.indexed_at) AS newest,count(*) OVER () AS group_total"
                + JOIN
                + " WHERE "
                + condition
                + f" GROUP BY t.{key} ORDER BY {ordering} LIMIT ? OFFSET ?",
                [*join, *params, query.limit, query.offset],
            )
            rows = list(await cursor.fetchall())
            if rows:
                total = int(rows[0]["group_total"])
            else:
                cursor = await conn.execute(
                    WITH_KEYS
                    + f"SELECT count(DISTINCT t.{key}) AS n"
                    + JOIN
                    + " WHERE "
                    + condition,
                    [*join, *params],
                )
                counted = await cursor.fetchone()
                total = int(counted["n"]) if counted else 0
            # Each listed group as a whole, in one statement for the page.
            cursor = await conn.execute(
                WITH_KEYS + f"SELECT t.{key} AS item_key,count(*) AS all_tracks,"
                f"sum({IMPORTED_SQL}) AS all_imported,sum({effective}) AS full_saved,"
                f"sum({RESTORABLE_SQL}) AS full_restorable"
                + JOIN
                + f" WHERE t.library_id=? AND ({pool})"
                f" AND t.{key} IN (SELECT value FROM json_each(?)) GROUP BY t.{key}",
                [*join, library, json.dumps([row["item_key"] for row in rows])],
            )
            whole = {
                group["item_key"]: {k: group[k] for k in FULL} for group in await cursor.fetchall()
            }
            nothing = dict.fromkeys(FULL, None) | {"all_tracks": 0}
            shown = [
                {
                    **{k: v for k, v in dict(row).items() if k != "group_total"},
                    **whole.get(row["item_key"], nothing),
                }
                for row in rows
            ]
            cursor = await conn.execute(
                WITH_KEYS + "SELECT count(*) AS all_count,"
                f"COALESCE(sum({effective}),0) AS saved_count,"
                f"COALESCE(sum({IMPORTED_SQL}),0) AS imported_count"
                + JOIN
                + f" WHERE t.library_id=? AND ({pool})",
                [*join, library],
            )
            counts = await cursor.fetchone()
            scope_label = None
            if query.scope == "song" and (query.album_id or query.artist_id):
                field = "album_id" if query.album_id else "artist_id"
                scoped, values = scope_sql(field, query.album_id or query.artist_id)
                cursor = await conn.execute(
                    WITH_KEYS
                    + "SELECT min(t.album) AS album,min(t.artist) AS artist"
                    + JOIN
                    + f" WHERE t.library_id=? AND ({pool}) AND ({scoped})",
                    [*join, library, *values],
                )
                scope_label = await cursor.fetchone()
            cursor = await conn.execute(
                "SELECT mode='legacy_shared' AS legacy_shared"
                " FROM collection_library_policy WHERE library_id=?",
                [library],
            )
            library_policy = await cursor.fetchone()
        return {
            "rows": shown,
            "total": total,
            "counts": {
                "all_count": counts["all_count"],
                "saved_count": counts["saved_count"],
                "unsaved_count": counts["all_count"] - counts["saved_count"],
            }
            if counts
            else {},
            "sources": {
                "imported_count": counts["imported_count"],
                "catalog_count": counts["all_count"] - counts["imported_count"],
            }
            if counts
            else {},
            "query": asdict(query),
            "scope_label": dict(scope_label) if scope_label else {},
            "legacy_shared": bool(library_policy and library_policy["legacy_shared"]),
        }

    async def operation(self, owner: str, ident: str) -> dict[str, Any] | None:
        row = await self.store.fetchone(
            "SELECT * FROM collection_operations WHERE owner_id=? AND id=?", [owner, ident]
        )
        return dict(row) if row else None

    async def freeze(self, op: dict[str, Any]) -> None:
        query = Query(**json.loads(op["query"]))
        selected = json.loads(op["selected"])
        selection = selection_of(op["owner_id"], op["profile_id"], str(op.get("selection") or ""))
        condition, params = where(op["library_id"], query, selection)
        join = joined(op["owner_id"], op["profile_id"], op["library_id"])
        key = KEYS[query.scope]
        chosen = ""
        if selected is not None:
            chosen = f" AND t.{key} IN (SELECT value FROM json_each(?))"
            params.append(json.dumps(selected))
        async with self.store.transaction() as conn:
            cursor = await conn.execute(
                "SELECT state FROM collection_operations WHERE id=?", [op["id"]]
            )
            current = await cursor.fetchone()
            if not current or current["state"] != "preparing":
                return
            await conn.execute(
                "DELETE FROM collection_operation_items WHERE operation_id=?", [op["id"]]
            )
            await conn.execute(
                WITH_KEYS
                + "INSERT INTO collection_operation_items(operation_id,track_key,before_saved,"
                "before_revision) SELECT ?,t.track_key,m.saved,COALESCE(m.revision,0)"
                + JOIN
                + " WHERE "
                + condition
                + chosen,
                [*join[:5], op["id"], *join[5:], *params],
            )
            cursor = await conn.execute(
                WITH_KEYS
                + f"SELECT count(*) AS n,COALESCE(sum(({saved_sql(selection)})<>?),0) AS affected,"
                f"count(DISTINCT t.{key}) AS group_count"
                + JOIN
                + " JOIN collection_operation_items i ON i.track_key=t.track_key"
                " WHERE t.library_id=? AND i.operation_id=?",
                [*join[:5], int(op["action"] == "save"), *join[5:], op["library_id"], op["id"]],
            )
            count = await cursor.fetchone()
            assert count
            affected = count["affected"]
            if op["action"] == "save":
                # A song added by hand brings its album: the album's songs without a choice
                # there come in too.
                cursor = await conn.execute(
                    WITH_KEYS + "SELECT count(*) AS n" + JOIN + " WHERE t.library_id=?"
                    " AND t.present=1 AND m.saved IS NULL AND NOT EXISTS (SELECT 1"
                    " FROM collection_operation_items x WHERE x.operation_id=?"
                    " AND x.track_key=t.track_key) AND t.album_id IN (SELECT r.album_id"
                    " FROM collection_operation_items i JOIN collection_tracks r"
                    " ON r.library_id=t.library_id AND r.track_key=i.track_key"
                    f" WHERE i.operation_id=?) AND NOT ({saved_sql(selection)})",
                    [*join, op["library_id"], op["id"], op["id"]],
                )
                more = await cursor.fetchone()
                affected += more["n"] if more else 0
            await conn.execute(
                "UPDATE collection_operations SET "
                "state='review',total=?,affected=?,group_count=? WHERE id=?",
                [count["n"], affected, count["group_count"], op["id"]],
            )
