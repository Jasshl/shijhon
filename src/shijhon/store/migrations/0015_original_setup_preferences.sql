-- Freeze the active selection revision for reviewed legacy conversion. Later app
-- library choices must never be overwritten when the administrator approves setup.
CREATE TABLE collection_original_setup_preferences (
    setup_id TEXT NOT NULL REFERENCES collection_original_setups(id),
    owner_id TEXT NOT NULL,
    active_profile_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY(setup_id,owner_id)
);
