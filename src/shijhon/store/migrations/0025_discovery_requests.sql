-- Keep discovery mix creation requests after their first run finishes or rules change.
CREATE TABLE playlist_discovery_requests (
    id TEXT PRIMARY KEY REFERENCES playlist_mixes(id) ON DELETE CASCADE,
    owner TEXT NOT NULL REFERENCES playlist_owners(id) ON DELETE CASCADE,
    request_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    UNIQUE(owner, request_key)
);
