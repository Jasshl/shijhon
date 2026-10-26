-- Delete agent work before its grant, including request tombstones whose batch
-- was already forgotten. Never turn an agent job into an unguarded owner job.
CREATE TRIGGER audio_agent_grant_delete BEFORE DELETE ON playlist_agent_tokens
BEGIN
    DELETE FROM audio_agent_requests WHERE creator=OLD.id;
    DELETE FROM audio_batches WHERE creator=OLD.id;
END;

-- Preparation can reuse the playlist connection's native credentials. Disconnect
-- removes that saved authorization too, in the same transaction as the grants.
-- Keep the owner row as a tombstone so preparation cannot silently reconnect.
CREATE TRIGGER audio_playlist_owner_disconnect AFTER DELETE ON playlist_owners
BEGIN
    UPDATE audio_owners SET authorization=NULL,generation=lower(hex(randomblob(16)))
        WHERE id=OLD.id;
    UPDATE audio_batches SET state='paused',error='Reconnect to resume preparation.'
        WHERE owner=OLD.id AND state IN ('queued','running');
    UPDATE audio_items SET state='paused' WHERE state='queued' AND batch IN
        (SELECT id FROM audio_batches WHERE owner=OLD.id);
END;
