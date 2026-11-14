-- The track lists of added releases, read again by the library pass (fill/track_lists.py):
-- when each release's list was last read from the catalog and what that reading led to. A
-- release without a row has not been read again since it was added; the row goes with its
-- release (taken out by the cleanup, a release added back starts anew).
CREATE TABLE release_refreshes (
    ref TEXT PRIMARY KEY REFERENCES releases (ref) ON DELETE CASCADE,
    checked_at REAL NOT NULL,            -- when the catalog was last asked for its list
    -- 'same': nothing to change; 'changed': tracks added or lengths corrected; 'pending':
    -- a dry run found tracks to add or lengths to correct (applied by the first run
    -- switched on);
    -- 'refused': the list cannot be used for this album (reason); 'gone': the catalog no
    -- longer has the release; 'failed': it could not be read, or the release not written
    -- now (asked again a day later); 'unwritten': a track or correction could not be
    -- written (asked again a week later).
    outcome TEXT NOT NULL CHECK (
        outcome IN ('same', 'changed', 'pending', 'refused', 'gone', 'failed', 'unwritten')
    ),
    added INTEGER NOT NULL DEFAULT 0,    -- tracks added as placeholders (pending: to add)
    updated INTEGER NOT NULL DEFAULT 0,  -- lengths corrected in place (pending: to correct)
    -- Differences left as they are: a song the catalog lists differently in another way
    -- than its length, a song with audio, a new track on a taken position or that may be
    -- a song the album has.
    left_out INTEGER NOT NULL DEFAULT 0,
    reason TEXT
);
CREATE INDEX release_refreshes_checked ON release_refreshes (checked_at);
