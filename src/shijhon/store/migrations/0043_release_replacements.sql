-- A filled album filled from another release the owner chose (the wrong edition was
-- filled). The row is written in the transaction that takes the old release's songs out
-- and makes the chosen release the album's plan, and goes once the album is filled from
-- it - or its match is decided otherwise. Until then Shijhon finishes the replacement
-- itself: at its next start, and after a failed fill a few times more (retry_at); when
-- that keeps failing (retry_at NULL) the plan stays, filled on the album's next use.
CREATE TABLE release_replacements (
    album_id TEXT PRIMARY KEY,           -- the owned Navidrome album
    scope TEXT NOT NULL,                 -- catalog and region of its match
    old_ref TEXT NOT NULL,               -- the release whose songs were taken out
    new_ref TEXT NOT NULL,               -- the release the owner chose
    started_at REAL NOT NULL,            -- wall-clock time
    attempts INTEGER NOT NULL DEFAULT 0, -- fills from the chosen release that failed
    retry_at REAL,                       -- when Shijhon tries again by itself
    reason TEXT NOT NULL DEFAULT ''      -- why the last fill failed
);

-- The Navidrome song IDs of placeholders such a replacement took out. Navidrome keeps
-- each as a missing song, and an app may still hold the ID: a use of one that reaches
-- Shijhon is never forwarded while no song has that ID (Navidrome would record the use
-- on the missing song). The new release's song takes an ID over where Navidrome gives it
-- the same one; the row then goes.
CREATE TABLE replaced_songs (
    song_id TEXT PRIMARY KEY,
    album_id TEXT NOT NULL,              -- the owned album it was in
    release_ref TEXT NOT NULL,           -- the release it was of
    replaced_at REAL NOT NULL
);
CREATE INDEX replaced_songs_album ON replaced_songs (album_id);
