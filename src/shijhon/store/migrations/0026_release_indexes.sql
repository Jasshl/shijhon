-- Cleanup, fills Undo and cascading release deletion look up children by release.
CREATE INDEX placeholders_release ON placeholders (release_ref);
CREATE INDEX track_links_release ON track_links (release_ref);
