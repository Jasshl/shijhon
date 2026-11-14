-- Catalog releases an agent was asked to add to its account's library
-- (``agents/library.py``): accepted once under the agent's idempotency key, then added
-- one after another by a worker, each with its outcome. ``creator``: the agent's grant
-- (``playlist_agent_tokens.id``), checked again before every release, so revoking the
-- agent stops what is left. ``items``: the releases as they were accepted, in order,
-- each with its state. ``state``: queued, running, finished, or stopped (the grant no
-- longer allows it; ``reason`` says so).
CREATE TABLE library_additions (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    creator TEXT NOT NULL,
    library TEXT NOT NULL,
    request_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    items TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    reason TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL,
    UNIQUE (owner, request_key)
);

CREATE INDEX library_additions_work ON library_additions (state, created);
