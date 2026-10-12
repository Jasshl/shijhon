-- The review reason an automatic pick settled (several plausible editions, or a release
-- numbered differently from the owned files); '' when the match was not picked.
ALTER TABLE album_matches ADD COLUMN picked TEXT NOT NULL DEFAULT '';
-- The owned albums a release is matched with (search results and artist pages ask).
CREATE INDEX album_matches_release ON album_matches (release_ref);
