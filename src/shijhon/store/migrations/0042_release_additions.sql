-- The daily cap on adding (``placeholders/daily_cap.py``): which account added which
-- catalog release to the library, and when. Written only while a cap is set, for accounts
-- it limits, in the transaction that records the release. The cap counts an account's
-- rows of the last 24 hours; within them a release added back needs no second place (the
-- key), and rows older than that are removed.
CREATE TABLE release_additions (
    user_id TEXT NOT NULL,
    release_ref TEXT NOT NULL,
    added_at REAL NOT NULL,
    PRIMARY KEY (user_id, release_ref)
) WITHOUT ROWID;

CREATE INDEX release_additions_recent ON release_additions (user_id, added_at);
