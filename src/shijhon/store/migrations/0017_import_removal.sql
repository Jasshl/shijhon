-- Removal keeps original bytes, native identities and personal provenance.
CREATE TABLE music_import_removals (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES music_import_jobs(id),
    owner_id TEXT NOT NULL,
    actor_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    include_used INTEGER NOT NULL CHECK(include_used IN (0,1)),
    cursor TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    approval_key TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL
);
CREATE TABLE music_import_removal_items (
    removal_id TEXT NOT NULL REFERENCES music_import_removals(id),
    item_id TEXT NOT NULL REFERENCES music_import_items(id),
    song_id TEXT NOT NULL,
    reasons TEXT NOT NULL,
    use_key TEXT NOT NULL,
    remove_file INTEGER NOT NULL CHECK(remove_file IN (0,1)),
    state TEXT NOT NULL DEFAULT 'present',
    PRIMARY KEY(removal_id,item_id)
);
CREATE INDEX music_import_removal_queue ON music_import_removals(state,created);
