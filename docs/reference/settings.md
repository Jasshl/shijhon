# Settings reference

The settings most people change are in [configuration.md](../configuration.md#the-settings),
with which value applies and how environment variables are named. An empty variable sets
nothing.

## Settings saved on the dashboard

- They are kept in Shijhon's database (in `state_dir`, mode 0600), secrets included.
  "Use the configuration's" next to a secret removes the saved secret.
- Most apply at once, and the cleanup's from its next check. These apply after a restart:
  - every `[catalog]` setting except `artwork_size`;
  - `[delivery] delivered_days`, `delivered_gb`, `user_routings` and `user_downloads`;
  - `[fill] enabled`, `complete_lists`, `pass_start_seconds` and
    `pass_requests_per_second`, and switching `library_pass` to or from `off`.
- A saved section that no longer fits the configuration, or with which Shijhon could not
  start (a saved file path whose file is gone, say), is not applied as a whole. Shijhon
  starts with the configuration's values for that section and says so: a warning at
  startup, and "Saved settings not in use" on the page.
- Saved values also apply while the dashboard is off.
- On a page with several forms, sending one loses unsaved changes in the others.

### The add-on list: the file's or the dashboard's

- With `[[addons]]` in the file, the file owns the list: at each start the add-ons are
  added, updated and ordered to match it, and stored ones it no longer names are disabled.
- Once you change the list on the dashboard, the stored list is kept and the file's is no
  longer applied at startup. The log and the Add-ons page say so. "Use the file's list
  after a restart" on that page hands it back.
- A list from `SHIJHON_ADDONS` always applies, and the dashboard shows it read-only.
- Without a list in the file or the environment, the stored list is kept.

### What the dashboard shows of stored values

- A secret is never shown. An add-on's setting is a secret when its manifest declares it
  one, when it is free text the manifest does not mark `"secret": false`, or when its name
  looks like a key, a token, an address or an account and it is no choice or switch.
- Choices and on/off switches are shown, unless typed into a hidden field. A number or
  plain text is shown only when it was typed into its visible field on the page; one from
  the file is used, and can be replaced or removed, but is not displayed.
- Secrets belong to the address they were entered for: when an add-on's address changes to
  another host, enter them again. The same holds for the settings a catalog adapter
  declares as belonging to an address it names.

## The other settings

Defaults in brackets. 0 switches a limit off where the table says so.

### Top level, `[server]`, `[navidrome]`, `[placeholders]`

| Setting | What it sets |
|---|---|
| `state_dir` (`var`) | Shijhon's database, downloads under way and the cover cache |
| `log_level` (`INFO`), `log_debug` (`[]`) | The log level; loggers logged at debug level whatever it says, e.g. `["shijhon.covers"]`: one line per catalog cover request |
| `[server] host` (`127.0.0.1`), `port` (8765) | Where Shijhon listens |
| `[server] dashboard` (on) | Off: `/shijhon/` goes to Navidrome like any other path |
| `[server] restart_by_supervisor` (off) | On: the dashboard offers **Restart Shijhon** ([deployment.md](../deployment.md#restarting)), for a service manager that starts Shijhon again after it exits (systemd: `Restart=always` or `Restart=on-failure`). Not needed in a container, where the restart policy does it |
| `[server] reverse_proxy_user_header` (`Remote-User`), `reverse_proxy_auth` (off) | Sign-in by a header from the reverse proxy ([advanced deployment](advanced-deployment.md#sign-in-by-the-reverse-proxy)) |
| `[server] credential_cache_seconds` (60) | How long an accepted login is reused |
| `[server] max_parsed_body_bytes` (1 MiB) | How much of a request body that is no form Shijhon reads; a larger one streams through to Navidrome |
| `[navidrome] database_path` | Navidrome's database, read by the cleanup instead of the usage export ([advanced deployment](advanced-deployment.md#reading-navidromes-database-directly)). With both set, the export is read |
| `[navidrome] purge_missing` | Set to `"never"` when Navidrome hides its configuration ([deployment.md](../deployment.md#what-shijhon-needs-from-navidrome)) |
| `[navidrome] timeout_seconds` (30), `client_name` (`shijhon`) | Shijhon's own requests to Navidrome |
| `[placeholders] folder` (`_shijhon`) | The placeholder folder inside the library: a folder of its own, not the library itself |

### `[delivery]`

| Setting | What it sets |
|---|---|
| `max_attempts` (2) | How many add-ons get a timed attempt at a song. One that lacks the song, or fails at once, does not count |
| `primary_budget_seconds` (4.5) | With `primary_first`: how long the primary has before an add-on that has the song ready takes over |
| `availability_timeout_seconds` (2) | How long an add-on has to say whether it has a song ready |
| `reliable_lookup_after_seconds` (0.5) | With `primary_first`: the likely fallback looks the song up, without playing it, once the primary's lookup has taken this long. 0: with every play |
| `prepare_when_not_ready` (off) | With `ready_first`: when no add-on has a song ready, ask one to prepare it for next time |
| `primary_miss_hours` (24), `primary_release_miss_minutes` (60) | How long a song the primary lacked is not asked for there again, and how long the other songs of its release look the fallback up at once. 0: off |
| `retry_skip_seconds` (60) | A retry of a song that failed skips the add-ons that failed for it within this time. 0: off |
| `length_tolerance_seconds` (10), `length_tolerance_percent` (5) | Audio whose length differs from the catalog's by more than the larger of the two is another recording: the next add-on plays. Both 0: no check |
| `cooldown_seconds` (30), `cooldown_errors` (3) | An add-on that answers with this many errors in a row, no more than ten minutes apart, is passed over for this long. 0 errors: off |
| `primary_cooldown_timeouts` (2), `primary_cooldown_switches` (3) | The primary is passed over after this many timeouts, or switches away from it, without a delivery in between |
| `request_timeout_seconds` (10), `seek_timeout_seconds` (15), `download_timeout_seconds` (180) | One request to an add-on; a jump within a playing song; fetching a whole song |
| `pin_ttl_seconds` (1800) | How long a song keeps its add-on and link, for seeks and repeats - also for a play under way when the song is put in the library |
| `warm_ahead_depth` (2), `warm_ahead_delay_seconds` (2), `warm_ahead_budget_seconds` (60), `warm_ahead_jobs` (2) | Warm-ahead: how many of the next songs (in the app's saved queue, else the album) are opened once a song plays, how long after its first audio, how long one may take, and how many jobs run at once for all listeners. Depth 0: off |
| `prefetch_memory_seconds` (600) | An app that fetches the next songs itself gets no warm-ahead for this long |
| `ahead_window_seconds` (0.5) | A stream that starts this soon after another song on the same app, and is not reported as playing, is a fetch ahead. 0: every stream is treated alike |
| `user_routings` (4), `user_downloads` (2), `user_downloads_per_hour` (120), `user_download_burst` (4) | The limits per user ([the add-on protocol](add-on-protocol.md#limits)). 0: no limit; a burst of 0: the whole hour's downloads in a row |
| `addon_requests_per_second` (2), `addon_request_burst` (4), `addon_audio_openings` (4) | The limits per add-on. 0 requests a second, or 0 openings: no limit. The burst is 1 or more |
| `dash` (on) | Play add-ons' DASH links ([add-ons.md](../add-ons.md#dash-links)). Needs ffmpeg on the `PATH`. Off: the next add-on is asked |
| `dash_start` (`"at_once"`) | `"at_once"`: a DASH song plays from its first segments, served as one MP4 file of them. `"complete"`: it plays once all its segments are joined into a FLAC, M4A or MP3 file. Songs saved to the library are joined files either way |
| `dash_segments_at_once` (4) | A DASH song's segments fetched at the same time |
| `dash_joins_at_once` (3) | DASH songs fetched or joined at the same time, for all listeners together. The songs being played never wait; more of the others wait their turn |
| `dash_cache_mb` (1024) | DASH songs kept for seeks and later plays; past this size the least recently used go first. The newest is always kept |

### `[catalog]` and `[search]`

The `[catalog]` settings are also on the Catalog page; these `[search]` settings are
set in the file or the environment.

| Setting | What it sets |
|---|---|
| `cache_seconds` (3600), `timeout_seconds` (15) | How long a catalog answer is reused; how long one request may take |
| `artwork_size` (1200) | The cover written into the library for a catalog album, in pixels |
| `artwork_cache_mb` (1024), `artwork_cache_days` (30) | The cache of catalog covers in `state_dir`. 0 MB: none kept on disk |
| `cover_sizes` | Covers are fetched at the next of these sizes up from the one asked for. Empty: the exact size |
| `prefetch_covers` (50), `prefetch_parallel` (4), `prefetch_burst_pages` (4), `prefetch_burst_seconds` (10) | The covers of the first catalog items of an artist page or search result are fetched ahead, this many at a time. An app shown that many pages within that time gets none meanwhile. 0 covers: off |
| `[search] min_query_length` (3) | Shorter searches get only Navidrome's answer |
| `[search] artist_pages` (on) | Catalog releases on your library's artist pages |
| `[search] discography_max_age_days` (7) | Saved artist discographies and top songs are refreshed in the background after this |
| `[search] artist_sync_pages` (10), `artist_sync_seconds` (10) | An app that opens this many artist pages without a saved discography within this time is syncing: those pages get only the library's answer |
| `[search] guard_searches` (60), `guard_window_seconds` (20), `guard_pause_seconds` (60) | An app that sends this many different searches within the window gets only the library's answers for the pause |
| `[search] song_only_additions` (on), `song_settle_seconds` (0.15) | Catalog results for searches that ask for songs only, and how long such a search waits before the catalog is asked |
| `[search] song_burst_searches` (4), `song_burst_seconds` (0.5), `song_burst_pause_seconds` (2) | This many different song-only searches within this time are a burst, answered from the library until the app has been quiet for the pause |

### `[fill]`

| Setting | What it sets |
|---|---|
| `complete_lists` (on) | An album shown complete carries the complete song count and length in album lists too. Off: lists keep Navidrome's counts until the fill |
| `open_budget_seconds` (3) | How long the first view of an album waits for its match; after it the view has only your songs, and the match continues in the background |
| `sync_albums` (10), `sync_seconds` (10) | An app that views this many never-matched albums within this time is syncing: those are matched in the background |
| `retry_hours` (1), `background_pause_seconds` (5) | After a failed match (then doubling); between background matches |
| `pass_start_seconds` (60), `pass_interval_hours` (6), `pass_requests_per_second` (1) | The library pass starts this long after startup, looks for new albums this often, and sends the catalog this many requests a second |
