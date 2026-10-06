-- Catalog releases that have placeholders (or link owned songs) in the library.
CREATE TABLE releases (
    ref TEXT PRIMARY KEY,                -- "<catalog>:<id>"
    folder TEXT NOT NULL UNIQUE,         -- library-relative folder of its placeholders
    album_id TEXT NOT NULL,              -- Navidrome album ID after the verified scan
    owned_album_id TEXT,                 -- set when the release completes an owned album
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    album_tags TEXT NOT NULL,            -- JSON: album-level tags every placeholder shares
    data TEXT NOT NULL,                  -- JSON snapshot of the catalog release
    created_at REAL NOT NULL,
    removing_at REAL                     -- being taken out (a stop: it is put back)
);

-- Every catalog track of a materialized release and the Navidrome song it maps to:
-- an owned song (owned = 1) or a placeholder (owned = 0).
CREATE TABLE track_links (
    track_ref TEXT PRIMARY KEY,
    song_id TEXT NOT NULL,
    release_ref TEXT NOT NULL REFERENCES releases (ref) ON DELETE CASCADE,
    owned INTEGER NOT NULL
);
CREATE INDEX track_links_song ON track_links (song_id);

CREATE TABLE placeholders (
    song_id TEXT PRIMARY KEY,            -- Navidrome song ID (kept across replacement)
    path TEXT NOT NULL UNIQUE,           -- library-relative path of the current file
    placeholder_path TEXT NOT NULL,      -- where the silent file lives when reverted
    track_ref TEXT NOT NULL UNIQUE,
    release_ref TEXT NOT NULL REFERENCES releases (ref) ON DELETE CASCADE,
    isrc TEXT,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    disc INTEGER NOT NULL,
    track INTEGER NOT NULL,
    tags TEXT NOT NULL,                  -- JSON: the placeholder's Vorbis comments
    state TEXT NOT NULL DEFAULT 'placeholder' CHECK (state IN ('placeholder', 'delivered')),
    delivered_at REAL,
    backing_song_id TEXT,                -- owned recording that plays for this placeholder
    created_at REAL NOT NULL,
    last_used_at REAL                    -- the last stream or download Shijhon served
);
CREATE INDEX placeholders_isrc ON placeholders (isrc);

-- Audio add-ons (the add-on protocol), tried in position order. base_url and settings may
-- hold secrets: they are never shown back in full. The budget and the limits: NULL means
-- the installation's ([delivery] budget_seconds, addon_requests_per_second,
-- addon_request_burst, addon_audio_openings).
CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused: cooldowns are kept by ID
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    settings TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL,
    reach TEXT NOT NULL DEFAULT 'public' CHECK (reach IN ('public', 'loopback', 'private')),
    created_at REAL NOT NULL,
    budget_seconds REAL,                 -- its own time to first audio
    requests_per_second REAL,            -- API requests a second
    request_burst INTEGER,               -- requests at once after a quiet moment
    audio_openings INTEGER               -- audio openings at once
);

-- Artist discographies for artist pages: a saved list answers at once and is
-- refreshed in the background once it is older than the configured age.
CREATE TABLE discographies (
    key TEXT PRIMARY KEY,                -- catalog, region and what was looked up,
                                         -- e.g. "demo.us:name:<folded artist name>"
    releases TEXT NOT NULL,              -- JSON: the releases as the catalog listed them
    fetched_at REAL NOT NULL             -- wall-clock time of the lookup
);

-- Every album a client opens is checked against these.
CREATE INDEX releases_album ON releases (album_id);
CREATE INDEX releases_owned_album ON releases (owned_album_id);

-- Owned albums matched to catalog releases: each album once per catalog and region. The
-- review list is the albums whose outcome is 'review'. A match can be a plan not acted on
-- yet (planned = 1, what the library pass would do); an album matched automatically but
-- owned too little to fill is kept as 'deferred' (its plan filled when it is opened); the
-- owner can keep an album as it is ('kept').
CREATE TABLE album_matches (
    album_id TEXT NOT NULL,              -- the owned Navidrome album
    scope TEXT NOT NULL,                 -- catalog and region, e.g. "demo.us"
    outcome TEXT NOT NULL
        CHECK (outcome IN ('filled', 'complete', 'review', 'none', 'failed', 'kept',
                           'deferred')),
    release_ref TEXT,                    -- the release matched (filled, complete, review)
    reason TEXT NOT NULL DEFAULT '',
    candidates TEXT NOT NULL DEFAULT '', -- the plausible releases, comma-separated
    attempts INTEGER NOT NULL DEFAULT 0, -- failures in a row (the wait doubles each time)
    checked_at REAL NOT NULL,            -- wall-clock time
    planned INTEGER NOT NULL DEFAULT 0,  -- 1: a dry run's outcome, not acted on yet
    plan TEXT,                           -- JSON, planned and deferred fills
    title TEXT NOT NULL DEFAULT '',      -- the owned album, for the lists
    artist TEXT NOT NULL DEFAULT '',
    owned_songs INTEGER NOT NULL DEFAULT 0,
    release_tracks INTEGER NOT NULL DEFAULT 0,
    cleaned_at REAL,                     -- the cleanup took its fill out: filled again
                                         -- only on its next use, never automatically
    PRIMARY KEY (album_id, scope)
);
CREATE INDEX album_matches_outcome ON album_matches (scope, outcome);

-- Values saved in the dashboard: they win over the configuration file; the
-- environment wins over them. value is JSON; secrets are stored here too (the database is
-- 0600) and are never shown back.
CREATE TABLE saved_settings (
    section TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    saved_at REAL NOT NULL,
    saved_by TEXT NOT NULL,
    PRIMARY KEY (section, key)
);

-- Dashboard sign-ins. Only a hash of the session cookie is kept. A session ends at
-- expires_at, or when unused for a while (used_at).
CREATE TABLE dashboard_sessions (
    token_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    csrf TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL NOT NULL
);

-- Releases the cleanup took out. A release being taken out is marked first
-- (releases.removing_at): a stop in the middle puts it back at the next start. One taken
-- out is kept as a record - its rows as they were - so that a request with one of its old
-- IDs adds it back at the same paths, with the same song IDs.
CREATE TABLE removed_releases (
    ref TEXT PRIMARY KEY,
    album_id TEXT NOT NULL,              -- its Navidrome album (a fill's: the owned album)
    owned_album_id TEXT,                 -- set for a fill of an owned album
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    record TEXT NOT NULL,                -- JSON: its releases, track_links, placeholders rows
    created_at REAL NOT NULL,            -- when it had been added
    removed_at REAL NOT NULL,
    restoring_at REAL                    -- being added back (a stop: its files go again)
);
CREATE TABLE removed_songs (
    song_id TEXT PRIMARY KEY,            -- a removed placeholder's Navidrome song ID
    release_ref TEXT NOT NULL REFERENCES removed_releases (ref) ON DELETE CASCADE
);
CREATE INDEX removed_songs_release ON removed_songs (release_ref);

-- The add-ons' recent attempts at byte zero, by which the fallbacks are ordered, kept
-- across restarts: each source's last 50 within a week - what its availability check
-- had said ("ready", "not now", or "-": it cannot tell, has no check, had not answered),
-- whether its audio's first byte came, and the seconds the attempt took.
CREATE TABLE source_attempts (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    at REAL NOT NULL,                    -- wall clock
    answer TEXT NOT NULL,
    delivered INTEGER NOT NULL,
    seconds REAL NOT NULL
);
CREATE INDEX source_attempts_source ON source_attempts (source_id, at);

-- New placeholders being written: recorded before their files move into the library,
-- deleted in the transaction that records the placeholders - or once their files are gone
-- again. A row a stop left behind is undone at the next start (its files go and a targeted
-- scan confirms it) and before its release is written again; until then its songs are
-- never forwarded to Navidrome as owned songs.
CREATE TABLE pending_placeholders (
    release_ref TEXT PRIMARY KEY,        -- one write of a release at a time (its lock)
    folder TEXT NOT NULL,                -- library-relative folder of the release
    paths TEXT NOT NULL,                 -- JSON: the new files' library-relative paths
    cover INTEGER NOT NULL,              -- 1: a new catalog album's cover.jpg with them
    new_folder INTEGER NOT NULL,         -- 1: the folder was new (removed again when empty)
    started_at REAL NOT NULL
);

-- Uses of a release that a check saw in Navidrome's records: a playlist
-- entry, a play queue, a bookmark or a share can be removed again, and Navidrome's database
-- then shows no trace of it. A use seen once keeps its release from then on: "anything
-- used even once is kept" also for uses that ended. (Plays, favorites and ratings leave
-- their own record in Navidrome; streams and downloads in Shijhon's placeholders.)
CREATE TABLE seen_uses (
    release_ref TEXT NOT NULL,           -- the release in the library (releases.ref)
    reason TEXT NOT NULL,                -- as the check words it: "in a playlist", ...
    seen_at REAL NOT NULL,               -- when a check first saw it
    PRIMARY KEY (release_ref, reason)
);
