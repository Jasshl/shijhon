# ruff: noqa: S608 -- SQL identifiers are constants or validated; values are bound.
"""The library policy, shared by both native and REST queries.

Each account has three built-in libraries; albums in them are whole:

- **Original imported library**: the account's own taste - its imports, the albums of the
  playlists it imported through Shijhon and its favorites (``original_albums``).
- **Shijhon library**: what Shijhon added from the catalog because of the account - the
  catalog albums its apps played, favorited, rated, put into a playlist, downloaded,
  shared or bookmarked, the audio it prepared and the playlists Shijhon published for it
  (``shijhon_albums``).
- **All**, the default in apps: the two together.

Each also keeps the account's own hand choices on the Collection page, and a hand choice
wins in its library. Custom libraries hold hand choices only. Another account's activity
never changes an account's libraries."""

from __future__ import annotations

import hashlib
import re

from shijhon.collection.contracts import MODES, CollectionError, Mode, Predicate

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
_EXPRESSION = re.compile(r"(?:[A-Za-z_][A-Za-z_0-9]*\.)?[A-Za-z_][A-Za-z_0-9]*\Z")
# Credit of an import whose song the library already had (``music_imports/overlap.py``):
# the importer's own import, also when that song is a catalog song Shijhon added.
LINK_GLOB = "link:*"


def own_sql(track: str, credit: str) -> str:
    """Whether a song is the owner's own import: an original credited to them (or every
    original while originals are shared, ``legacy_shared``; ``lp`` is the library policy)
    or a song their import was linked to. ``track``/``credit``: the aliases of
    ``collection_tracks`` and the owner's ``collection_originals`` row (LEFT JOINed, so
    never NULL: ``NOT`` of it must stay true or false)."""
    return (
        f"(({track}.original=1 AND (lp.mode='legacy_shared' OR {credit}.owner_id IS NOT NULL))"
        f" OR coalesce({credit}.provenance_id,'') GLOB '{LINK_GLOB}')"
    )


# The membership profile that holds each built-in library's hand choices: All's is the
# account's original default profile.
_BUILTIN_PREFIX = {"all": "default-", "original": "original-", "shijhon": "shijhon-"}


def default_profile_id(owner_id: str) -> str:
    """All's membership profile."""
    return builtin_profile_id(owner_id, "all")


def builtin_profile_id(owner_id: str, selection: str) -> str:
    """The membership profile of a built-in library (``all``, ``original``, ``shijhon``)."""
    return _BUILTIN_PREFIX[selection] + hashlib.sha256(owner_id.encode()).hexdigest()[:32]


def builtin_selection(owner_id: str, profile_id: str) -> str:
    """The built-in library whose membership profile ``profile_id`` is ('' if none)."""
    return next((s for s in _BUILTIN_PREFIX if builtin_profile_id(owner_id, s) == profile_id), "")


def real_audio_predicate(
    library_id: str, *, song_expression: str = "id", database: str = "main"
) -> Predicate:
    """The songs of the library whose audio is in it, for an account that gets real audio
    only ([placeholders] real_audio_accounts): an owned or imported file, or a placeholder
    whose delivered audio is in place - never a silent placeholder (also not one an owned
    recording plays for: that recording is listed itself), nor a file in the placeholder
    folder that no placeholder row tells of (one being written, or left by a stop). The
    placeholder rows are read in the same snapshot as the list they select from.
    Independent of the account's library choices."""
    if not _IDENTIFIER.fullmatch(database) or not _EXPRESSION.fullmatch(song_expression):
        raise CollectionError("Invalid collection query identifier.")
    return Predicate(
        f"EXISTS (SELECT 1 FROM {database}.collection_tracks ct "
        f"WHERE ct.library_id=? AND ct.song_id={song_expression} AND ct.present=1 "
        f"AND NOT EXISTS (SELECT 1 FROM {database}.placeholders p "
        "WHERE p.song_id=ct.song_id AND p.state<>'delivered') "
        f"AND (ct.original=1 OR EXISTS (SELECT 1 FROM {database}.placeholders p "
        "WHERE p.song_id=ct.song_id AND p.state='delivered')) "
        f"AND EXISTS (SELECT 1 FROM {database}.collection_index ci "
        "WHERE ci.library_id=ct.library_id AND ci.generation<>'' AND ci.updated_at>0))",
        (library_id,),
    )


def _bound(
    template: str, owner_id: str, library_id: str, keys: tuple[str, str] | None
) -> Predicate:
    """``template`` with its ``{owner}`` and ``{library}`` bound: as parameters, or as the
    SQL expressions ``keys`` (owner, library) - then the subquery has no parameters."""
    if keys is not None:
        return Predicate(template.format(owner=keys[0], library=keys[1]), ())
    order = [m.group(1) for m in re.finditer(r"\{(owner|library)\}", template)]
    return Predicate(
        template.format(owner="?", library="?"),
        tuple(owner_id if name == "owner" else library_id for name in order),
    )


def _chosen(d: str, profile: str) -> str:
    """A ``UNION`` term of a library's albums: those with a song the account added to it by
    hand (``profile``: a condition on ``hm.profile_id``). A hand addition brings its whole
    album; a hand removal of a song still leaves that song out."""
    return (
        f" UNION SELECT r.album_id FROM {d}.collection_membership hm JOIN {d}.collection_tracks r"
        " ON r.library_id=hm.library_id AND r.track_key=hm.track_key"
        " WHERE hm.owner_id={owner} AND hm.library_id={library} AND hm.saved=1"
        f" AND {profile}"
    )


def original_albums(
    owner_id: str,
    library_id: str,
    *,
    database: str = "main",
    keys: tuple[str, str] | None = None,
) -> Predicate:
    """The albums of ``library_id`` in ``owner_id``'s Original imported library by its rule,
    as one uncorrelated subquery (one row per album; SQLite reads it once per statement):
    those with one of the account's imports (every original while the library's originals
    are shared, ``legacy_shared``; a song its import was linked to), with a song or album it
    favorited (``collection_favorites``), with a song of a playlist it imported
    (``collection_imported``), or with a song it added there by hand."""
    if not _IDENTIFIER.fullmatch(database):
        raise CollectionError("Invalid collection query identifier.")
    d = database
    return _bound(
        f"SELECT r.album_id FROM {d}.collection_tracks r JOIN {d}.collection_library_policy rp"
        " ON rp.library_id=r.library_id WHERE r.library_id={library} AND r.original=1"
        " AND r.present=1 AND rp.mode='legacy_shared'"
        f" UNION SELECT r.album_id FROM {d}.collection_originals ro JOIN {d}.collection_tracks r"
        " ON r.library_id=ro.library_id AND r.track_key=ro.track_key"
        " WHERE ro.owner_id={owner} AND ro.library_id={library} AND r.present=1"
        f" AND (r.original=1 OR ro.provenance_id GLOB '{LINK_GLOB}')"
        f" UNION SELECT r.album_id FROM {d}.collection_favorites f JOIN {d}.collection_tracks r"
        " ON r.library_id=f.library_id AND r.song_id=f.item_id AND r.present=1"
        " WHERE f.owner_id={owner} AND f.library_id={library} AND f.kind='song'"
        f" UNION SELECT f.item_id FROM {d}.collection_favorites f"
        " WHERE f.owner_id={owner} AND f.library_id={library} AND f.kind='album'"
        f" UNION SELECT r.album_id FROM {d}.collection_imported i JOIN {d}.collection_tracks r"
        " ON r.library_id=i.library_id AND r.song_id=i.song_id AND r.present=1"
        " WHERE i.owner_id={owner} AND i.library_id={library}"
        + _chosen(d, f"hm.profile_id GLOB '{_BUILTIN_PREFIX['original']}*'"),
        owner_id,
        library_id,
        keys,
    )


def shijhon_albums(
    owner_id: str,
    library_id: str,
    *,
    database: str = "main",
    keys: tuple[str, str] | None = None,
) -> Predicate:
    """The albums of ``library_id`` in ``owner_id``'s Shijhon library by its rule, as one
    uncorrelated subquery: those with a catalog song the account's activity recorded
    (``collection_history.activity``: its apps' plays, favorites, ratings, playlists,
    downloads, shares and bookmarks, its favorites read from Navidrome, the playlists
    Shijhon published for it and the audio it prepared), the catalog albums carried over at
    the upgrade that introduced these libraries when they were given to the account
    (migration 0024, ``collection/carryover.py``), and those with a song it added there by
    hand."""
    if not _IDENTIFIER.fullmatch(database):
        raise CollectionError("Invalid collection query identifier.")
    d = database
    return _bound(
        f"SELECT r.album_id FROM {d}.collection_history h JOIN {d}.collection_tracks r"
        " ON r.library_id=h.library_id AND r.track_key=h.track_key AND r.present=1"
        " AND r.original=0 WHERE h.owner_id={owner} AND h.library_id={library}"
        " AND h.activity=1"
        # From the carried-over albums, when given to the account, to their songs (CROSS
        # JOIN: in this order).
        f" UNION SELECT r.album_id FROM {d}.collection_carryover_owner a"
        f" CROSS JOIN {d}.collection_carryover c CROSS JOIN {d}.collection_tracks r"
        " ON r.release_key=c.release_ref AND r.library_id={library} AND r.present=1"
        " WHERE a.owner_id={owner}"
        + _chosen(d, f"hm.profile_id GLOB '{_BUILTIN_PREFIX['shijhon']}*'"),
        owner_id,
        library_id,
        keys,
    )


def chosen_albums(
    owner_id: str,
    library_id: str,
    profile: str,
    *,
    database: str = "main",
    keys: tuple[str, str] | None = None,
) -> Predicate:
    """The albums with a song the account added by hand to All (``profile`` 'all') or to a
    custom library (``profile``: an SQL expression of its ID), as one subquery."""
    if not _IDENTIFIER.fullmatch(database):
        raise CollectionError("Invalid collection query identifier.")
    condition = (
        f"hm.profile_id GLOB '{_BUILTIN_PREFIX['all']}*'"
        if profile == "all"
        else f"hm.profile_id={profile}"
    )
    return _bound(_chosen(database, condition).removeprefix(" UNION "), owner_id, library_id, keys)


def predicate(
    owner_id: str,
    library_id: str,
    *,
    song_expression: str = "id",
    database: str = "main",
    mode: Mode | None = None,
    profile_id: str | None = None,
) -> Predicate:
    """Collection policy inside the caller's SQLite snapshot.

    Intersect current native permissions BEFORE counts/order/pagination. When mode or
    profile is omitted, SQL resolves the active profile and its mode inside the same
    read transaction as the native list/count. No Python pre-read races activation.

    Each built-in library is whole albums by its rule, then its own hand choices: a hand
    addition or removal of a song wins in that library. Original imported library: the
    account's imports and ``original_albums``; Shijhon library: ``shijhon_albums``; All
    (the default): Original imported library and Shijhon library as the account left them,
    then All's own hand choices. A custom library holds its hand choices only. A song added
    by hand brings its whole album into that library. No visible profile is created by this
    predicate. Explicit foreign/archived/missing profiles fail closed. Values are always
    bound; the only SQL interpolation is validated [alias.]column and database identifiers.
    """
    if not _IDENTIFIER.fullmatch(database) or not _EXPRESSION.fullmatch(song_expression):
        raise CollectionError("Invalid collection query identifier.")
    if mode is not None and mode not in MODES:
        raise CollectionError("Choose Original imported library, Shijhon library or All.")
    d = database
    own = own_sql("ct", "co")
    selected = profile_id
    if mode is not None:
        selected = {"original": "original", "saved": "shijhon", "all": "all"}[mode]
    original = original_albums(owner_id, library_id, database=d)
    shijhon = shijhon_albums(owner_id, library_id, database=d)
    in_all = chosen_albums(owner_id, library_id, "all", database=d)
    # The selection, again where a subquery must not depend on a row (read once).
    selection = (
        f"(SELECT COALESCE(?,(SELECT active_profile_id FROM {d}.collection_preferences"
        " WHERE owner_id=?),'all'))"
    )
    custom = chosen_albums(owner_id, library_id, selection, database=d)
    sql: list[str] = []
    params: list[str | None] = []

    def add(text: str, *values: str | None) -> None:
        sql.append(text)
        params.extend(values)

    # Per song only a few index lookups: the selection (and whether it is a custom
    # library) and the account's hand choices in its three built-in libraries are each read
    # once per statement, the choices as one set (``ch``) keyed by song.
    choice = {
        column: f"max(CASE WHEN m.profile_id GLOB '{_BUILTIN_PREFIX[library]}*' THEN m.saved END)"
        for column, library in (("a", "all"), ("o", "original"), ("s", "shijhon"))
    }
    builtin = " OR ".join(f"m.profile_id GLOB '{prefix}*'" for prefix in _BUILTIN_PREFIX.values())
    add(
        "EXISTS (SELECT 1 FROM (SELECT v.id,(SELECT 1 FROM "
        f"{d}.collection_profiles cp WHERE cp.owner_id=? AND cp.id=v.id AND cp.kind='custom' "
        f"AND cp.archived=0) AS custom FROM (SELECT {selection} AS id) v) cv "
        f"JOIN {d}.collection_tracks ct "
        f"JOIN {d}.collection_library_policy lp ON lp.library_id=ct.library_id "
        f"LEFT JOIN {d}.collection_originals co ON co.owner_id=? "
        "AND co.library_id=ct.library_id AND co.track_key=ct.track_key ",
        owner_id,
        selected,
        owner_id,
        owner_id,
    )
    add(
        f"LEFT JOIN (SELECT m.track_key,{choice['a']} AS a,{choice['o']} AS o,{choice['s']} AS s "
        f"FROM {d}.collection_membership m WHERE m.owner_id=? AND m.library_id=? "
        f"AND ({builtin}) GROUP BY m.track_key) ch ON ch.track_key=ct.track_key ",
        owner_id,
        library_id,
    )
    add(
        f"WHERE ct.library_id=? AND ct.song_id={song_expression} AND ct.present=1 ",
        library_id,
    )
    # Each rule appears once (its subquery is read once per statement): All's own hand
    # choice first, then Original imported library's and Shijhon library's rules with
    # their own hand choices, and the albums added to All by hand - each only where the
    # selection holds it. A custom library: its hand choices and their albums.
    add(
        "AND CASE WHEN cv.id IN ('original','shijhon','all') THEN COALESCE("
        "CASE WHEN cv.id='all' THEN ch.a END,"
        f"(cv.id IN ('original','all') AND COALESCE(ch.o,{own} OR ct.album_id IN "
        f"({original.sql}))) OR (cv.id IN ('shijhon','all') AND COALESCE(ch.s,"
        f"ct.album_id IN ({shijhon.sql}))) OR (cv.id='all' AND ct.album_id IN ({in_all.sql})))",
        *original.params,
        *shijhon.params,
        *in_all.params,
    )
    add(
        " ELSE cv.custom IS NOT NULL AND COALESCE((SELECT mc.saved FROM "
        f"{d}.collection_membership mc WHERE mc.owner_id=? AND mc.profile_id=cv.id "
        "AND mc.library_id=ct.library_id AND mc.track_key=ct.track_key),"
        f"ct.album_id IN ({custom.sql}))=1 END AND EXISTS (SELECT 1 FROM {d}.collection_index ci "
        "WHERE ci.library_id=? AND ci.generation<>'' AND ci.updated_at>0))",
        owner_id,
        *custom.params,
        selected,
        owner_id,
        library_id,
    )
    return Predicate("".join(sql), tuple(params))
