-- Authorization and frozen native snapshots are separate from discovery mixes.
CREATE TABLE audio_owners (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    authorization TEXT,
    generation TEXT NOT NULL,
    library_id INTEGER NOT NULL
);
CREATE TABLE audio_batches (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL REFERENCES audio_owners(id),
    generation TEXT NOT NULL,
    library_id INTEGER NOT NULL,
    playlist_id TEXT NOT NULL,
    title TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    created REAL NOT NULL,
    state TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE audio_items (
    batch TEXT NOT NULL REFERENCES audio_batches(id) ON DELETE CASCADE,
    song_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    entries INTEGER NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    track_ref TEXT,
    state TEXT NOT NULL DEFAULT 'queued',
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(batch, song_id)
);
CREATE INDEX audio_work ON audio_items(state, batch, position);
CREATE INDEX audio_owner_batches ON audio_batches(owner, created);
CREATE UNIQUE INDEX audio_active_playlist ON audio_batches(owner, playlist_id)
    WHERE state IN ('queued','running','cancelling');
