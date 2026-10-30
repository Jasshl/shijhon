-- A refused private-file deletion keeps its pending receipt and reservation, but
-- does not block cleanup of other files or retry on every worker turn/restart.
ALTER TABLE music_import_items ADD COLUMN received_cleanup_error TEXT NOT NULL DEFAULT '';
