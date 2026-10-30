-- Unpublished received bytes are disposable only after their job is terminal.
-- Keep the report/receipt row and its reservation until unlink and directory fsync
-- finish. A pending deletion is repeated safely after an interrupted cleanup.
ALTER TABLE music_import_items ADD COLUMN received_cleanup TEXT NOT NULL DEFAULT ''
    CHECK(received_cleanup IN ('','pending','deleted'));
CREATE INDEX music_import_removal_item ON music_import_removal_items(item_id);
