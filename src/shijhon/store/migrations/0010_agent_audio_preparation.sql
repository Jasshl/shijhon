-- Agent preparation uses the existing audio worker and native owner snapshots.
-- Grants survive credential rotation. No existing grant gains a permission.
ALTER TABLE audio_batches ADD COLUMN creator TEXT REFERENCES playlist_agent_tokens(id);
CREATE TABLE audio_agent_requests (
    creator TEXT NOT NULL REFERENCES playlist_agent_tokens(id),
    owner TEXT NOT NULL REFERENCES audio_owners(id),
    library_id INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    playlist_id TEXT NOT NULL,
    batch TEXT REFERENCES audio_batches(id) ON DELETE SET NULL,
    PRIMARY KEY (creator, idempotency_key)
);
CREATE INDEX audio_agent_batches ON audio_batches(creator, created);
