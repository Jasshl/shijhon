"""What keeps a release's metadata from unused-release cleanup: an explicit personal save,
and a song of it an active import is linked to (the importer's own import,
``music_imports/overlap.py``). Audio eviction is independent."""

SAVED_RELEASE_SQL = (
    "SELECT 1 FROM collection_membership m JOIN collection_tracks t"
    " ON t.library_id=m.library_id AND t.track_key=m.track_key"
    " WHERE t.release_key=? AND m.saved=1 LIMIT 1"
)
IMPORTED_RELEASE_SQL = (
    "SELECT 1 FROM music_import_links l JOIN placeholders p ON p.song_id=l.song_id"
    " WHERE p.release_ref=? AND l.active=1 LIMIT 1"
)
# Each query (one parameter: the release's ref) with the reason the cleanup gives.
KEPT_RELEASE = (
    (SAVED_RELEASE_SQL, "saved in a collection"),
    (IMPORTED_RELEASE_SQL, "an imported song"),
)
