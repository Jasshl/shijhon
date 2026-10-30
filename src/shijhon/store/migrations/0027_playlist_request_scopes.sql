-- Existing requests retain their current grant ceiling; new requests capture the token.
ALTER TABLE playlist_requests ADD COLUMN scopes TEXT NOT NULL DEFAULT '[]';
UPDATE playlist_requests SET scopes=coalesce(
    (SELECT scopes FROM playlist_agent_tokens WHERE id=playlist_requests.creator), '[]'
) WHERE creator!='';
