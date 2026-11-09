-- What decides which songs a library selection holds (``collection/policy.py``) counts its
-- changes here, in the transaction that makes them: the private list cache keeps each
-- selection's songs and reads them for as long as these counters stand still, instead of
-- working the selection out again for every statement. Scopes: '' for what every account's
-- selection reads, 'owner:<id>' for one account's choices and records, 'placeholders' for
-- what the real-audio selection reads besides. Written by triggers, so every writer counts.
CREATE TABLE collection_revisions (
    scope TEXT PRIMARY KEY,
    revision INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TRIGGER collection_tracks_inserted_revision AFTER INSERT ON collection_tracks BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_tracks_deleted_revision AFTER DELETE ON collection_tracks BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_tracks_updated_revision AFTER UPDATE OF library_id,track_key,song_id,release_key,album_id,original,present ON collection_tracks BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;

CREATE TRIGGER collection_index_inserted_revision AFTER INSERT ON collection_index BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_index_deleted_revision AFTER DELETE ON collection_index BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
-- A scan that finds nothing new only moves updated_at, which the selection reads as set or not.
CREATE TRIGGER collection_index_updated_revision AFTER UPDATE OF library_id,generation,updated_at ON collection_index
WHEN OLD.library_id IS NOT NEW.library_id OR OLD.generation IS NOT NEW.generation
    OR (OLD.updated_at>0) IS NOT (NEW.updated_at>0) BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;

CREATE TRIGGER collection_library_policy_inserted_revision AFTER INSERT ON collection_library_policy BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_library_policy_deleted_revision AFTER DELETE ON collection_library_policy BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_library_policy_updated_revision AFTER UPDATE OF library_id,mode ON collection_library_policy BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;

CREATE TRIGGER collection_carryover_inserted_revision AFTER INSERT ON collection_carryover BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_carryover_deleted_revision AFTER DELETE ON collection_carryover BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_carryover_updated_revision AFTER UPDATE OF release_ref ON collection_carryover BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;

CREATE TRIGGER collection_carryover_owner_inserted_revision AFTER INSERT ON collection_carryover_owner BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_carryover_owner_deleted_revision AFTER DELETE ON collection_carryover_owner BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_carryover_owner_updated_revision AFTER UPDATE OF owner_id ON collection_carryover_owner BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;

CREATE TRIGGER collection_originals_inserted_revision AFTER INSERT ON collection_originals BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || NEW.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_originals_deleted_revision AFTER DELETE ON collection_originals BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || OLD.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_originals_updated_revision AFTER UPDATE OF owner_id,library_id,track_key,provenance_id ON collection_originals BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || NEW.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;

CREATE TRIGGER collection_favorites_inserted_revision AFTER INSERT ON collection_favorites BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || NEW.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_favorites_deleted_revision AFTER DELETE ON collection_favorites BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || OLD.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_favorites_updated_revision AFTER UPDATE OF owner_id,library_id,kind,item_id ON collection_favorites BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || NEW.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;

CREATE TRIGGER collection_imported_inserted_revision AFTER INSERT ON collection_imported BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || NEW.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_imported_deleted_revision AFTER DELETE ON collection_imported BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || OLD.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_imported_updated_revision AFTER UPDATE OF owner_id,library_id,song_id ON collection_imported BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || NEW.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;

CREATE TRIGGER collection_membership_inserted_revision AFTER INSERT ON collection_membership BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || NEW.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_membership_deleted_revision AFTER DELETE ON collection_membership BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || OLD.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_membership_updated_revision AFTER UPDATE OF owner_id,profile_id,library_id,track_key,saved ON collection_membership BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || NEW.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;

CREATE TRIGGER collection_profiles_inserted_revision AFTER INSERT ON collection_profiles BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || NEW.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_profiles_deleted_revision AFTER DELETE ON collection_profiles BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || OLD.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_profiles_updated_revision AFTER UPDATE OF id,owner_id,kind,archived ON collection_profiles BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || NEW.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;

CREATE TRIGGER collection_preferences_inserted_revision AFTER INSERT ON collection_preferences BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || NEW.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_preferences_deleted_revision AFTER DELETE ON collection_preferences BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || OLD.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_preferences_updated_revision AFTER UPDATE OF owner_id,active_profile_id ON collection_preferences BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || NEW.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;

CREATE TRIGGER collection_history_inserted_revision AFTER INSERT ON collection_history BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || NEW.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_history_deleted_revision AFTER DELETE ON collection_history BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || OLD.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER collection_history_updated_revision AFTER UPDATE OF owner_id,library_id,track_key,activity ON collection_history
WHEN OLD.activity IS NOT NEW.activity OR OLD.track_key IS NOT NEW.track_key OR OLD.library_id IS NOT NEW.library_id OR OLD.owner_id IS NOT NEW.owner_id BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('owner:' || NEW.owner_id,1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;

CREATE TRIGGER placeholders_inserted_revision AFTER INSERT ON placeholders BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('placeholders',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER placeholders_deleted_revision AFTER DELETE ON placeholders BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('placeholders',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER placeholders_updated_revision AFTER UPDATE OF song_id,state ON placeholders BEGIN
    INSERT INTO collection_revisions(scope,revision) VALUES ('placeholders',1)
        ON CONFLICT(scope) DO UPDATE SET revision=revision+1;
END;
-- What a cover's ID is checked against, for every cover a page of the library shows. The
-- ID comes first: no other query of the library's songs should find this index usable.
CREATE INDEX collection_tracks_cover ON collection_tracks(json_extract(metadata,'$.coverArt'),library_id)
    WHERE present=1;
