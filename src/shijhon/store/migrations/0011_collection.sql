-- Durable identities have no foreign keys into materialized release/placeholder tables:
-- metadata cleanup must not erase personal collection choices.
CREATE TABLE collection_tracks (
    library_id TEXT NOT NULL,
    track_key TEXT NOT NULL,
    song_id TEXT NOT NULL,
    release_key TEXT NOT NULL,
    album_id TEXT NOT NULL,
    artist_id TEXT NOT NULL,
    title TEXT NOT NULL,
    album TEXT NOT NULL,
    artist TEXT NOT NULL,
    search_text TEXT NOT NULL,
    duration REAL NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0,
    year INTEGER NOT NULL DEFAULT 0,
    metadata TEXT NOT NULL DEFAULT '{}',
    original INTEGER NOT NULL CHECK(original IN (0,1)),
    present INTEGER NOT NULL DEFAULT 1 CHECK(present IN (0,1)),
    generation TEXT NOT NULL,
    indexed_at REAL NOT NULL,
    PRIMARY KEY(library_id,track_key)
);
CREATE INDEX collection_tracks_native ON collection_tracks(library_id,song_id,present);
CREATE UNIQUE INDEX collection_tracks_present_native ON collection_tracks(library_id,song_id) WHERE present=1;
CREATE TABLE collection_track_aliases (
    library_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    track_key TEXT NOT NULL,
    PRIMARY KEY(library_id,alias)
);
CREATE TABLE collection_scan_aliases (
    library_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    alias TEXT NOT NULL,
    track_key TEXT NOT NULL,
    PRIMARY KEY(library_id,generation,alias)
);
CREATE INDEX collection_tracks_album ON collection_tracks(library_id,album_id,present);
CREATE INDEX collection_tracks_artist ON collection_tracks(library_id,artist_id,present);
CREATE INDEX collection_tracks_release ON collection_tracks(release_key);
CREATE TABLE collection_scan_tracks AS SELECT * FROM collection_tracks WHERE 0;
CREATE UNIQUE INDEX collection_scan_identity ON collection_scan_tracks(library_id,generation,track_key);
CREATE UNIQUE INDEX collection_scan_native ON collection_scan_tracks(library_id,generation,song_id);
CREATE TABLE collection_profiles (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'custom' CHECK(kind IN ('custom','builtin')),
    name TEXT NOT NULL COLLATE NOCASE,
    base_original INTEGER NOT NULL DEFAULT 1 CHECK(base_original IN (0,1)),
    mode TEXT NOT NULL DEFAULT 'saved' CHECK(mode IN ('saved','original','all')),
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    UNIQUE(owner_id,id),
    UNIQUE(owner_id,name)
);
CREATE TABLE collection_preferences (
    owner_id TEXT PRIMARY KEY,
    active_profile_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE collection_membership (
    owner_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    library_id TEXT NOT NULL,
    track_key TEXT NOT NULL,
    saved INTEGER CHECK(saved IN (0,1)),
    revision INTEGER NOT NULL,
    last_operation TEXT NOT NULL,
    PRIMARY KEY(owner_id,profile_id,library_id,track_key),
    FOREIGN KEY(owner_id,profile_id) REFERENCES collection_profiles(owner_id,id)
);
CREATE INDEX collection_membership_saved ON collection_membership(library_id,saved,track_key);
CREATE TABLE collection_index (
    library_id TEXT PRIMARY KEY,
    generation TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'empty',
    indexed INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE collection_operations (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    library_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('save','remove','undo')),
    state TEXT NOT NULL,
    query TEXT NOT NULL,
    selected TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    total INTEGER NOT NULL DEFAULT 0,
    affected INTEGER NOT NULL DEFAULT 0,
    group_count INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    changed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    approved_at REAL,
    created REAL NOT NULL,
    UNIQUE(owner_id,profile_id,library_id,request_key),
    FOREIGN KEY(owner_id,profile_id) REFERENCES collection_profiles(owner_id,id)
);
CREATE TABLE collection_operation_items (
    operation_id TEXT NOT NULL REFERENCES collection_operations(id) ON DELETE CASCADE,
    track_key TEXT NOT NULL,
    before_saved INTEGER,
    before_revision INTEGER NOT NULL DEFAULT 0,
    after_revision INTEGER,
    processed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(operation_id,track_key)
);
CREATE INDEX collection_operation_pending ON collection_operation_items(operation_id,processed);
