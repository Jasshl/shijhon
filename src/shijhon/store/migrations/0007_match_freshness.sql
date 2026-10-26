ALTER TABLE album_matches ADD COLUMN match_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE album_matches ADD COLUMN owned_fingerprint TEXT;
