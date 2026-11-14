-- New releases of the artists an account follows (``new_releases.py``): each person's own
-- choice, off until they turn it on. Everything here is that account's own and goes when
-- it turns the feature off.

-- The accounts that turned it on. ``username``: the account's name when it last used
-- the dashboard (apps' requests name accounts); the account itself is ``owner_id``.
-- ``state``: why the last check did not finish ('': it did): 'sign_in' (the account's
-- saved sign-in was not accepted), 'unavailable' (Navidrome or the catalog failed).
CREATE TABLE new_release_accounts (
    owner_id TEXT PRIMARY KEY,           -- Navidrome user ID
    username TEXT NOT NULL,
    enabled_at REAL NOT NULL,
    checked_at REAL,                     -- the last check that finished
    next_at REAL NOT NULL DEFAULT 0,     -- when the next check is due
    state TEXT NOT NULL DEFAULT '' CHECK (state IN ('', 'sign_in', 'unavailable'))
);

-- The artists an account follows, as its last check read them from Navidrome.
-- ``favorite``: the artist, or an album or song of the artist, is a favorite of the
-- account; ``played``: the artist is among its most played.
CREATE TABLE new_release_artists (
    owner_id TEXT NOT NULL REFERENCES new_release_accounts (owner_id) ON DELETE CASCADE,
    artist_id TEXT NOT NULL,             -- Navidrome artist ID
    name TEXT NOT NULL,
    favorite INTEGER NOT NULL DEFAULT 0,
    played INTEGER NOT NULL DEFAULT 0,
    checked_at REAL,                     -- when the artist's releases were last looked at
    PRIMARY KEY (owner_id, artist_id)
);

-- The releases found for an account, by the followed artist they were found for (a
-- release of two followed artists has two rows and shows once).
CREATE TABLE new_releases (
    owner_id TEXT NOT NULL REFERENCES new_release_accounts (owner_id) ON DELETE CASCADE,
    artist_id TEXT NOT NULL,
    ref TEXT NOT NULL,                   -- "<catalog>:<album id>"
    scope TEXT NOT NULL,                 -- the catalog and region it was found in
    release_date TEXT NOT NULL,          -- as the catalog gave it
    data TEXT NOT NULL,                  -- the release (catalog.model.release_data)
    found_at REAL NOT NULL,
    PRIMARY KEY (owner_id, artist_id, ref)
);
