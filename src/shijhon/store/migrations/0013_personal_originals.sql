-- Physical classification stays in collection_tracks. Import credit is durable,
-- many-to-many and independent of disposable catalog rows and native accounts.
CREATE TABLE collection_originals (
    owner_id TEXT NOT NULL,
    library_id TEXT NOT NULL,
    track_key TEXT NOT NULL,
    provenance_id TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY(owner_id,library_id,track_key)
);
CREATE INDEX collection_originals_track ON collection_originals(library_id,track_key);
CREATE TABLE collection_library_policy (
    library_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK(mode IN ('legacy_shared','personal')),
    revision INTEGER NOT NULL CHECK(revision > 0),
    setup_id TEXT
);
CREATE TRIGGER collection_personal_sticky BEFORE UPDATE OF mode ON collection_library_policy
WHEN OLD.mode='personal' AND NEW.mode<>'personal'
BEGIN
    SELECT RAISE(ABORT,'Personal library policy cannot be downgraded');
END;

-- Internal, explicitly reviewed setup. No ownership is inferred by migration.
CREATE TABLE collection_original_setups (
    id TEXT PRIMARY KEY,
    library_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    legacy_owner_id TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('review','applied')),
    created REAL NOT NULL,
    applied_at REAL
);
CREATE TABLE collection_original_setup_accounts (
    setup_id TEXT NOT NULL REFERENCES collection_original_setups(id),
    owner_id TEXT NOT NULL,
    PRIMARY KEY(setup_id,owner_id)
);
CREATE TABLE collection_original_setup_tracks (
    setup_id TEXT NOT NULL REFERENCES collection_original_setups(id),
    track_key TEXT NOT NULL,
    song_id TEXT NOT NULL,
    PRIMARY KEY(setup_id,track_key)
);
CREATE TABLE collection_original_setup_profiles (
    setup_id TEXT NOT NULL REFERENCES collection_original_setups(id),
    owner_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    PRIMARY KEY(setup_id,owner_id,profile_id)
);
