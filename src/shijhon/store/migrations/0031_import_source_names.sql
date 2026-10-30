-- Keep the original allocated destination when an overlap review points at a copy.
ALTER TABLE music_import_items ADD COLUMN source_name TEXT NOT NULL DEFAULT '';
UPDATE music_import_items SET source_name=CASE WHEN state='joined'
    THEN 'batch-'||job_id||'/'||display_path ELSE final_name END
    WHERE derived_from='';
