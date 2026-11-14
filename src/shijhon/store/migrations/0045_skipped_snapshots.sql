-- An upgrade of the schema that went on without the snapshot ``upgrade_snapshots`` asked
-- for, because it could not be written (``store/skipped.py``): one record, written by the
-- start that upgraded, shown on the dashboard and notified once, removed when an
-- administrator dismisses it. ``reason``: why, as a kind (no_room, unwritable, damaged,
-- unreadable, copy_check, exists, error); ``needed`` and ``free``: bytes, for no_room;
-- ``code``: the system's error number, when there was one; ``error``: the class of another
-- error. Never the text of an exception.
CREATE TABLE skipped_snapshots (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    from_schema INTEGER NOT NULL,
    to_schema INTEGER NOT NULL,
    at REAL NOT NULL,
    reason TEXT NOT NULL,
    needed INTEGER,
    free INTEGER,
    code INTEGER,
    error TEXT NOT NULL DEFAULT ''
);
