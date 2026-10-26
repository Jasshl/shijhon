-- OAuth tokens refer to durable, owner/library-scoped connection grants. Refreshing
-- credentials must not change a request's creator or stop its scheduled refreshes.
ALTER TABLE playlist_agent_tokens ADD COLUMN oauth_client TEXT NOT NULL DEFAULT '';
CREATE INDEX playlist_agent_oauth_owner ON playlist_agent_tokens(owner,oauth_client);
CREATE TABLE agent_oauth_clients (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE agent_oauth_flows (
    id TEXT PRIMARY KEY,
    client TEXT NOT NULL REFERENCES agent_oauth_clients(id) ON DELETE CASCADE,
    params TEXT NOT NULL,
    csrf TEXT NOT NULL DEFAULT '',
    expires REAL NOT NULL
);
CREATE TABLE agent_oauth_codes (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    grant_id TEXT NOT NULL REFERENCES playlist_agent_tokens(id) ON DELETE CASCADE,
    expires REAL NOT NULL
);
CREATE TABLE agent_oauth_tokens (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('access', 'refresh')),
    client TEXT NOT NULL REFERENCES agent_oauth_clients(id) ON DELETE CASCADE,
    grant_id TEXT NOT NULL REFERENCES playlist_agent_tokens(id) ON DELETE CASCADE,
    resource TEXT NOT NULL,
    scopes TEXT NOT NULL,
    expires REAL NOT NULL,
    used REAL NOT NULL DEFAULT 0
);
CREATE INDEX agent_oauth_tokens_grant ON agent_oauth_tokens(grant_id);
