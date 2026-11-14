-- Artist lists ([catalog] artist_lists): songs whose files name several artists in one
-- text ("A, B"), which Navidrome makes one artist of. One row a song - ``key``: its
-- Navidrome ID, or "import:<item>" for a music import's file (listed when it is published,
-- before it has one) -, what was found for it and, once its file has the list, what undoes
-- it. ``kind``:
-- 'file': a library file Shijhon did not write; 'import': the published file of a music
--   import (``item_id``), replaced by a copy with the list (the file it was stays
--   retained as ``staging_before``, the copy is ``staging_after``);
-- 'placeholder': one of Shijhon's own silent files, given the list by the credit repair
--   (only what the add-on catalogs said is kept here: the file's tags are its row's).
-- ``state``:
-- 'written': the file has the list (``artists``, and ``album_artists`` when every song of
--   its album has the same list) and, without an album artist tag before, the album
--   artist Navidrome showed; ``previous`` names those tags (they were absent). Its bytes
--   then were ``size_after``/``sha_after``; before, ``size_before``/``sha_before``, and its
--   tags as Navidrome read them had the digest ``raw_before``;
-- 'unresolved': no catalog gave a list its text is exactly; 'ambiguous': catalogs gave
--   different lists; 'skipped': the file cannot take it (``reason``); 'failed': a write
--   was rolled back (``reason``): each asked again from ``retry_at``;
-- 'declined': the file changed after Shijhon wrote it: left alone for good;
-- 'undone': the list taken back (the setting turned off), the row going with its backup.
-- ``source``: 'link' (the catalog item linked to the song), 'isrc' or 'search' (an add-on
-- catalog). ``phase``: a write or an undo under way ('write', 'undo') or a backup still to
-- go ('drop'), with the names of its files in ``work``: the next start finishes it.
CREATE TABLE artist_lists (
    key TEXT PRIMARY KEY,
    song_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'import', 'placeholder')),
    path TEXT NOT NULL,                  -- library-relative, when last looked at
    item_id TEXT,                        -- the music import's item
    artist TEXT NOT NULL,                -- the joined text, as Navidrome showed it
    state TEXT NOT NULL CHECK (state IN
        ('written', 'unresolved', 'ambiguous', 'skipped', 'failed', 'declined', 'undone')),
    reason TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    artists TEXT,                        -- JSON list
    album_artists TEXT,                  -- JSON list
    previous TEXT,                       -- JSON {tag: null}: the tags given, absent before
    album_id TEXT,
    size_before INTEGER,
    sha_before TEXT,
    raw_before TEXT,
    size_after INTEGER,
    sha_after TEXT,
    staging_before TEXT,
    staging_after TEXT,
    phase TEXT NOT NULL DEFAULT '' CHECK (phase IN ('', 'write', 'undo', 'drop')),
    work TEXT,
    updated_at REAL NOT NULL,
    retry_at REAL
);
CREATE INDEX artist_lists_state ON artist_lists (state, retry_at);
CREATE INDEX artist_lists_item ON artist_lists (item_id) WHERE item_id IS NOT NULL;
CREATE INDEX artist_lists_phase ON artist_lists (phase) WHERE phase <> '';
