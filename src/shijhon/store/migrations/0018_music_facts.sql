-- Optional hints for playlist curation; both tables are caches that can be rebuilt.
-- Facts from the sound analyzer, keyed by Navidrome song ID; raw values, percentiles
-- are computed when read.
CREATE TABLE sound_facts (
    song_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL DEFAULT '',
    tempo REAL,
    musical_key TEXT NOT NULL DEFAULT '',
    energy REAL,
    features TEXT NOT NULL DEFAULT '{}',
    labels TEXT NOT NULL DEFAULT '[]',
    synced REAL NOT NULL
);
-- Community tags by folded artist and title; an empty title holds the artist's tags.
CREATE TABLE music_tags (
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    tags TEXT NOT NULL,
    fetched REAL NOT NULL,
    PRIMARY KEY (artist, title)
);
