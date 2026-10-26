-- The bytes an import's audio in place of a catalog song takes (the placeholder engine's
-- copy carrying the song's tags, in the managed folder): reserved against the importer's
-- storage before the copy is made, released only once it is gone again.
ALTER TABLE music_import_links ADD COLUMN reserved INTEGER NOT NULL DEFAULT 0;
