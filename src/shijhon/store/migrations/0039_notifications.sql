-- The states notifications report (``notifications.py``), so that one is sent once when it
-- begins and not again while it lasts, across restarts too. ``key``: what the state is
-- about ("addon:<source id>", "cleanup:blocked", "cleanup:export", "import:<job id>",
-- "playlist:<mix id>"); ``event``: its kind of notification; ``active``: whether it lasts
-- now; ``began_at``: when it last began; ``sent_at``: when a notification about it last
-- went out (NULL: none), which also spaces out one that ends and begins again. Rows of
-- states that ended are removed once that spacing has passed.
CREATE TABLE notification_states (
    key TEXT PRIMARY KEY,
    event TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    began_at REAL NOT NULL,
    sent_at REAL
);
