-- Standalone requests reuse the durable playlist runs and native publisher.
CREATE TABLE playlist_agent_tokens (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL REFERENCES playlist_owners(id) ON DELETE CASCADE,
    library TEXT NOT NULL,
    name TEXT NOT NULL,
    scopes TEXT NOT NULL,
    created REAL NOT NULL,
    revoked REAL NOT NULL DEFAULT 0
);
CREATE TABLE playlist_requests (
    id TEXT PRIMARY KEY REFERENCES playlist_mixes(id) ON DELETE CASCADE,
    owner TEXT NOT NULL REFERENCES playlist_owners(id) ON DELETE CASCADE,
    creator TEXT NOT NULL,
    library TEXT NOT NULL,
    request_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    input TEXT NOT NULL,
    first_run TEXT NOT NULL REFERENCES playlist_runs(id) ON DELETE CASCADE,
    UNIQUE(owner, request_key)
);
