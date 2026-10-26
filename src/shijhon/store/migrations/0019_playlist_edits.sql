-- Songs the owner removed from or added to a playlist Shijhon refreshes, found before a
-- refresh by comparing the native playlist with the last publication (``publication``,
-- a run ID). One row per playlist and song (artist and title): the latest edit counts.
CREATE TABLE playlist_edits (
    mix TEXT NOT NULL REFERENCES playlist_mixes(id) ON DELETE CASCADE,
    song_key TEXT NOT NULL,
    song_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('removed', 'added')),
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    album TEXT NOT NULL DEFAULT '',
    publication TEXT NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (mix, song_key)
);
CREATE INDEX playlist_edits_latest ON playlist_edits(mix, kind, at DESC);
