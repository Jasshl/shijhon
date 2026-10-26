-- Music already in the library is linked, not published again.
-- ``facts``: the probe's bounded tags and audio quality of the received file.
-- ``outcome``: what the review decided for it (``music_imports/overlap.py``).
-- ``match``: the evidence shown in the review and report (target song, qualities).
-- ``distinct``: the person said "Not the same song": publish it as a new song.
-- ``derived_from``: a published copy carrying an existing album's album-level tags;
-- the received original (that item) stays unchanged and retained.
ALTER TABLE music_import_items ADD COLUMN facts TEXT NOT NULL DEFAULT '';
ALTER TABLE music_import_items ADD COLUMN outcome TEXT NOT NULL DEFAULT '';
ALTER TABLE music_import_items ADD COLUMN match TEXT NOT NULL DEFAULT '';
ALTER TABLE music_import_items ADD COLUMN distinct_song INTEGER NOT NULL DEFAULT 0
    CHECK(distinct_song IN (0,1));
ALTER TABLE music_import_items ADD COLUMN derived_from TEXT NOT NULL DEFAULT '';
CREATE INDEX music_import_item_digest ON music_import_items(digest) WHERE digest<>'';
CREATE INDEX music_import_item_derived ON music_import_items(derived_from)
    WHERE derived_from<>'';
-- An import's songs that were already in the library. Its credit is a
-- ``collection_originals`` row with provenance ``link:<item>``, written only when the
-- owner had none for that song (``credited``); removing the import withdraws it,
-- restoring it gives it back.
CREATE TABLE music_import_links (
    item_id TEXT PRIMARY KEY REFERENCES music_import_items(id),
    job_id TEXT NOT NULL REFERENCES music_import_jobs(id),
    owner_id TEXT NOT NULL,
    library_id TEXT NOT NULL,
    track_key TEXT NOT NULL,
    song_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('library','catalog_song')),
    credited INTEGER NOT NULL CHECK(credited IN (0,1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created REAL NOT NULL
);
CREATE INDEX music_import_link_job ON music_import_links(job_id);
CREATE INDEX music_import_link_track ON music_import_links(library_id,track_key);
