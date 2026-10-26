-- What brings whole albums into an account's All library besides its own imports: a
-- song or album it favorited in Navidrome, and the songs of the playlists it imported.
-- Each account's own, learned from its own requests and reads.
-- Explicit Collection choices (collection_membership) are unchanged and still win.
CREATE TABLE collection_favorites (
    owner_id TEXT NOT NULL,
    library_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('song','album')),
    item_id TEXT NOT NULL,
    PRIMARY KEY(owner_id,library_id,kind,item_id)
);
-- When the account's favorites were last read from Navidrome as that account.
CREATE TABLE collection_favorites_read (
    owner_id TEXT NOT NULL,
    library_id TEXT NOT NULL,
    read_at REAL NOT NULL,
    PRIMARY KEY(owner_id,library_id)
);
-- The songs each playlist import published (its matched entries, not the owner's edits).
CREATE TABLE collection_imported (
    owner_id TEXT NOT NULL,
    library_id TEXT NOT NULL,
    import_id TEXT NOT NULL REFERENCES playlist_mixes(id) ON DELETE CASCADE,
    song_id TEXT NOT NULL,
    PRIMARY KEY(owner_id,library_id,import_id,song_id)
);
-- Songs added to a library's snapshot in place, in its generation (``Index.add``), by
-- addition: the query index's cached facts of that snapshot take in every addition they
-- lack before they are read, also after a stop between the two.
CREATE TABLE collection_index_additions (
    library_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    revision INTEGER NOT NULL,
    song_id TEXT NOT NULL,
    PRIMARY KEY(library_id,generation,revision,song_id)
);
-- The albums of original files, read for every All list.
CREATE INDEX collection_tracks_original_albums ON collection_tracks(library_id,album_id)
    WHERE original=1 AND present=1;
-- The built-in library a change edits: All and Shijhon library share their choices, and
-- only All counts the albums these reasons bring in. '' for custom libraries and for
-- changes made before.
ALTER TABLE collection_operations ADD COLUMN selection TEXT NOT NULL DEFAULT '';
-- Playlists imported before: the songs of each import's last publication.
INSERT OR IGNORE INTO collection_imported(owner_id,library_id,import_id,song_id)
SELECT i.owner,i.library,i.id,j.value FROM playlist_imports i
JOIN playlist_runs r ON r.id=(SELECT p.id FROM playlist_runs p
    WHERE p.mix=i.id AND p.state='published' ORDER BY p.created DESC,p.rowid DESC LIMIT 1)
JOIN json_each(CASE WHEN json_valid(r.data) THEN r.data ELSE '{}' END,'$.native_ids') j
WHERE j.type='text' AND j.value<>'';
