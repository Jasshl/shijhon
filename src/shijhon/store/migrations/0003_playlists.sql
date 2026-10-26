-- All taste, connections and runs belong to an immutable Navidrome user ID.
ALTER TABLE dashboard_sessions ADD COLUMN user_id TEXT NOT NULL DEFAULT '';
-- Old sessions did not record identity; sign in again to bind it.
DELETE FROM dashboard_sessions;
CREATE TABLE playlist_owners (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    connection TEXT NOT NULL,
    generation TEXT NOT NULL,
    history_until INTEGER NOT NULL DEFAULT 0,
    history_from INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE playlist_listens (
    owner TEXT NOT NULL REFERENCES playlist_owners(id) ON DELETE CASCADE,
    at INTEGER NOT NULL,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    album TEXT NOT NULL,
    PRIMARY KEY (owner, at, artist, title, album)
);
CREATE TABLE playlist_mixes (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL REFERENCES playlist_owners(id) ON DELETE CASCADE,
    definition TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    due REAL NOT NULL DEFAULT 0,
    playlist_id TEXT NOT NULL DEFAULT '',
    published TEXT NOT NULL DEFAULT ''
);
CREATE TABLE playlist_runs (
    id TEXT PRIMARY KEY,
    mix TEXT NOT NULL REFERENCES playlist_mixes(id) ON DELETE CASCADE,
    created REAL NOT NULL,
    state TEXT NOT NULL,
    automatic INTEGER NOT NULL DEFAULT 0 CHECK(automatic IN (0,1)),
    data TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX playlist_one_active ON playlist_runs(mix)
    WHERE state IN ('queued', 'generating', 'preview', 'publishing', 'uncertain');
CREATE INDEX playlist_run_history ON playlist_runs(mix, created DESC);
