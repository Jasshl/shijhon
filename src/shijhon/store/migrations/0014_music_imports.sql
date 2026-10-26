-- Private retained-original ledger. No dependency on disposable catalog records.
CREATE TABLE music_import_installation (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    id TEXT NOT NULL UNIQUE
);
CREATE TABLE music_import_jobs (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    library_id TEXT NOT NULL,
    root_id TEXT NOT NULL,
    config_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL DEFAULT '',
    approved_revision INTEGER,
    approval_key TEXT,
    approved_at REAL,
    error TEXT NOT NULL DEFAULT '',
    resume_state TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    UNIQUE(owner_id,request_key)
);
CREATE INDEX music_import_job_queue ON music_import_jobs(state,created);
CREATE TABLE music_import_pages (
    job_id TEXT NOT NULL REFERENCES music_import_jobs(id),
    page_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY(job_id,page_key)
);
CREATE TABLE music_import_items (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL REFERENCES music_import_jobs(id),
    client_key TEXT NOT NULL,
    display_path TEXT NOT NULL,
    expected_size INTEGER NOT NULL CHECK(expected_size>0),
    reserved_size INTEGER NOT NULL CHECK(reserved_size>=0),
    expected_digest TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    received_size INTEGER NOT NULL DEFAULT 0,
    digest TEXT NOT NULL DEFAULT '',
    staging_name TEXT NOT NULL,
    final_name TEXT NOT NULL UNIQUE,
    warnings TEXT NOT NULL DEFAULT '[]',
    review_excluded INTEGER NOT NULL DEFAULT 0 CHECK(review_excluded IN (0,1)),
    error TEXT NOT NULL DEFAULT '',
    song_id TEXT NOT NULL DEFAULT '',
    track_key TEXT NOT NULL DEFAULT '',
    UNIQUE(job_id,client_key)
);
CREATE INDEX music_import_item_state ON music_import_items(job_id,state);
CREATE UNIQUE INDEX music_import_native_receipt ON music_import_items(song_id)
    WHERE song_id<>'';
