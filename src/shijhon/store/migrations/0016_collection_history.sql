-- Personal additions outlive profiles, native IDs and materialized catalog rows.
-- No foreign key into disposable structural or placeholder metadata.
CREATE TABLE collection_history (
    owner_id TEXT NOT NULL,
    library_id TEXT NOT NULL,
    track_key TEXT NOT NULL,
    added_at REAL NOT NULL,
    catalog_data TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(owner_id,library_id,track_key)
);

-- Positive per-owner membership is evidence; a negative override alone is not.
INSERT OR IGNORE INTO collection_history(owner_id,library_id,track_key,added_at)
SELECT owner_id,library_id,track_key,0 FROM collection_membership WHERE saved=1;

-- Successful approved Save items remain evidence after later Remove/Undo/archive.
-- Never infer attribution from prepared, reviewed or merely approved items.
INSERT OR IGNORE INTO collection_history(owner_id,library_id,track_key,added_at)
SELECT o.owner_id,o.library_id,i.track_key,o.approved_at
FROM collection_operations o JOIN collection_operation_items i ON i.operation_id=o.id
WHERE o.action='save' AND o.approved_at IS NOT NULL
AND i.processed=1 AND i.after_revision IS NOT NULL;

-- Preserve a proven recording snapshot while its shared release still exists.
UPDATE collection_history AS h SET catalog_data=COALESCE((
    SELECT json_remove(j.value,'$.artwork_template') FROM collection_track_aliases a
    JOIN collection_tracks t ON t.library_id=a.library_id AND t.track_key=a.track_key
    JOIN releases r ON r.ref=t.release_key JOIN json_each(r.data,'$.tracks') j
    WHERE a.library_id=h.library_id AND a.track_key=h.track_key
    AND a.alias='catalog:' || json_extract(j.value,'$.ref.catalog') || ':' ||
        json_extract(j.value,'$.ref.id') LIMIT 1
),'{}');
