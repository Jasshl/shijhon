-- Immutable ordered input, separate from reviewed native publication checkpoints.
CREATE TABLE playlist_imports (
    id TEXT PRIMARY KEY REFERENCES playlist_mixes(id) ON DELETE CASCADE,
    owner TEXT NOT NULL REFERENCES playlist_owners(id) ON DELETE CASCADE,
    library TEXT NOT NULL,
    request_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    input TEXT NOT NULL,
    UNIQUE(owner, request_key)
);
