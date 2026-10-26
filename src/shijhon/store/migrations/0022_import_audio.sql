-- An import's own audio in place of a catalog song it matched (``placeholders.import_item``:
-- the import item whose received audio it is). That audio is the import's, not a cache:
-- delivery expiry never reverts it, and it goes when the import's link goes.
ALTER TABLE placeholders ADD COLUMN import_item TEXT;
-- Where an import's audio for its catalog song is: '' (no catalog song), 'pending' (to be
-- put in place), 'placed', 'kept' (the song has audio of its own, never overwritten),
-- 'untaggable' (its format cannot carry the song's tags), 'low_quality' (below the
-- importer's minimum), 'failed' (the swap was refused or rolled back).
ALTER TABLE music_import_links ADD COLUMN audio TEXT NOT NULL DEFAULT '';
CREATE INDEX music_import_link_audio ON music_import_links(audio) WHERE audio<>'';
-- A person's own imported song upgraded from the add-ons after they played it
-- (``music_imports/upgrade.py``): ``upgrade`` is the durable intent of a swap under way
-- (a stop rolls it back), ``upgraded_from`` the file it replaced (retained privately).
ALTER TABLE music_import_items ADD COLUMN upgrade TEXT NOT NULL DEFAULT '';
ALTER TABLE music_import_items ADD COLUMN upgraded_from TEXT NOT NULL DEFAULT '';
CREATE INDEX music_import_item_upgrade ON music_import_items(id) WHERE upgrade<>'';
