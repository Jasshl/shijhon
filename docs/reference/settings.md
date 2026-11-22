# Settings reference

The settings most people change are in [configuration.md](../configuration.md#the-settings),
with which value applies and how environment variables are named. An empty variable sets
nothing.

## Settings saved on the dashboard

- They are kept in Shijhon's database (in `state_dir`, mode 0600), secrets included.
  "Use the configuration's" next to a secret removes the saved secret.
- Most apply at once, and the cleanup's from its next check. These apply after a restart:
  - every `[catalog]` setting except `artwork_size` and `user_releases_per_day`;
  - `[delivery] delivered_days`, `delivered_gb`, `user_routings`, `user_downloads`, `dash`
    and `dash_cache_mb`;
  - `[fill] enabled`, `complete_lists`, `pass_start_seconds` and
    `pass_requests_per_second`, and switching `library_pass` to or from `off`;
  - every `[audiomuse]` setting;
  - `[agents] public_url` and `private_http`.
- Of `[playlists]`, only `allow_private_api` can be saved on the dashboard. It applies at
  once. The runner commands are set only in the file.
- A saved section that no longer fits the configuration, or with which Shijhon could not
  start (a saved file path whose file is gone, say), is not applied as a whole. Shijhon
  starts with the configuration's values for that section and says so: a warning at
  startup, and "Saved settings not in use" on the page.
- Saved values also apply while the dashboard is off.
- On a page with several forms, sending one loses unsaved changes in the others.

### The add-on list: the file's or the dashboard's

- With `[[addons]]` in the file, the file owns the list: at each start the add-ons are
  added, updated and ordered to match it, and stored ones it no longer names are disabled.
  A stored add-on renamed on the dashboard is found by the key it was given under the
  file's name, and takes that name again.
- Once you change the list on the dashboard, the stored list is kept and the file's is no
  longer applied at startup. The log says so, and a note at the end of the Add-ons page,
  whose "Use the file's list after a restart" hands it back.
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

## Remote agent access

`[agents] public_url` (empty): the canonical public HTTPS origin, without a path.
Set it and restart to enable OAuth and Streamable HTTP MCP at `/shijhon/mcp`.
An empty value disables MCP. HTTP is accepted only on loopback, or on a home network with
`[agents] private_http` ([agent access](../agent-access.md#turn-on-agent-access)). The environment
variable is `SHIJHON_AGENTS__PUBLIC_URL`. [Agent setup](../agent-access.md) lists the
required reverse-proxy routes and feature prerequisites.

## The other settings

Defaults in brackets. 0 switches a limit off where the table says so.

### Top level, `[server]`, `[navidrome]`, `[placeholders]`

| Setting | What it sets |
|---|---|
| `state_dir` (`var`) | Shijhon's database, the snapshots taken before upgrades (`snapshots`), backups made with `shijhon backup` (`backups`), downloads under way and the cover cache |
| `upgrade_snapshots` (on) | Before a start migrates the database to a newer schema, a checked copy of it goes into `snapshots` in `state_dir` ([upgrades](../deployment.md#upgrades), [in detail](advanced-deployment.md#snapshots-before-an-upgrade)). Off: the database is migrated without a copy, and nothing is shown or reported. In the file or the environment only (`SHIJHON_UPGRADE_SNAPSHOTS`) |
| `log_level` (`INFO`), `log_debug` (`[]`) | The log level (`CRITICAL`, `FATAL`, `ERROR`, `WARNING`, `WARN`, `INFO`, `DEBUG` or `NOTSET`; case and surrounding spaces ignored); loggers logged at debug level whatever it says, e.g. `["shijhon.covers"]`: one line per catalog cover request |
| `[server] host` (`127.0.0.1`), `port` (8765) | Where Shijhon listens; port 1–65535 |
| `[server] dashboard` (on) | Off: `/shijhon/` goes to Navidrome like any other path |
| `[server] restart_by_supervisor` (off) | On: the dashboard offers **Restart Shijhon** ([deployment.md](../deployment.md#restarting)), for a service manager that starts Shijhon again after it exits (systemd: `Restart=always` or `Restart=on-failure`). Not needed in a container, where the restart policy does it |
| `[server] reverse_proxy_user_header` (`Remote-User`), `reverse_proxy_auth` (off) | Sign-in by a header from the reverse proxy ([advanced deployment](advanced-deployment.md#sign-in-by-the-reverse-proxy)) |
| `[server] credential_cache_seconds` (60) | How long an accepted login is reused; finite and at least 0 (0 disables reuse) |
| `[server] max_parsed_body_bytes` (1 MiB) | How much of a request body that is no form Shijhon reads; a larger one streams through to Navidrome. At least 0 (0 passes non-form bodies through) |
| `[navidrome] socket` | Navidrome's Unix socket (its `Address = "unix:<path>"`), an absolute path, when Shijhon reaches it there. `url` must then be an `http://` address that names no machine to reach, such as `"http://navidrome.invalid"`: it gives the `Host` header of Shijhon's own requests (apps' requests keep theirs). With `ExtAuth.TrustedSources = "@"`, Navidrome trusts only that socket |
| `[navidrome] artist_participations` (`false`) | Match Navidrome's `Subsonic.ArtistParticipations` setting for artist album counts in personal library views. Set both to `true` if artists should include albums on which they perform as a track artist. Artist indexes still list album artists, as Navidrome does |
| `[navidrome] database_path` | Navidrome's database, read by the cleanup instead of the usage export ([advanced deployment](advanced-deployment.md#reading-navidromes-database-directly)). With both set, the export is read |
| `[navidrome] purge_missing` | Set to `"never"` when Navidrome hides its configuration ([deployment.md](../deployment.md#what-shijhon-needs-from-navidrome)) |
| `[navidrome] timeout_seconds` (30), `client_name` (`shijhon`) | Shijhon's own requests to Navidrome; timeout finite and greater than 0 |
| `[placeholders] folder` (`_shijhon`) | The placeholder folder inside the library: a folder of its own, not the library itself |
| `[placeholders] real_audio_accounts` (`[]`) | Navidrome user names (letter case does not matter) of accounts that see only songs whose audio is in the library, for a sound analyzer such as AudioMuse-AI ([how it works](../how-it-works.md#accounts-for-sound-analyzers)). Applies after a restart; `SHIJHON_PLACEHOLDERS__REAL_AUDIO_ACCOUNTS='["audiomuse"]'` in the environment |

### Music imports

The `[music_imports]` limits are in bytes, seconds or counts: `max_file_bytes`,
`max_job_bytes`, `max_job_files`, `max_owner_jobs`, `max_owner_bytes`,
`max_global_bytes`, `min_free_bytes`, `max_uploads`, `upload_seconds` and
`validation_seconds` ([their defaults](../music-imports.md#bounds)). These settings are
configured in TOML or the environment and take effect on restart. Existing files in the
imports folder stay in place; the personal library setup records which account imported
them and preserves other existing users' selections. Disabling uploads does not reset
ownership or existing collection choices.

### `[delivery]`

The main download controls are on **Playback → Downloads and playlist preparation**;
per-user and preparation controls are under **Advanced settings**. Values from the file,
saved dashboard settings and the environment keep the normal precedence.

Add-on transfer overrides are `[[addons]] downloads` and `preparations`; omitted means
inherit, a number means a custom limit, and 0 removes that cap. The dashboard displays
the inherited default and effective shared limit. `capacity_group` is a local label
(up to 100 characters) for add-ons sharing an account or worker. One add-on added twice
already shares its transfer limits; add-ons served at one address count separately unless
a named group connects them, at one address or several. The strictest nonzero limit
across connected enabled members applies. API request limits are per add-on. Standard manifests do not report backend independence or remaining
account quotas; configure the limits to suit the account.

| Setting | What it sets |
|---|---|
| `availability_timeout_seconds` (2) | How long an add-on has to say whether it has a song ready |
| `prepare_when_not_ready` (off) | With `ready_checks`: when no add-on has a song ready, ask one to prepare it for next time |
| `miss_hours` (24) | How long a song an add-on lacked is not asked of it again. 0: off |
| `retry_skip_seconds` (60) | A retry of a song that failed skips the add-ons that failed for it within this time. 0: off |
| `length_tolerance_seconds` (10), `length_tolerance_percent` (5) | Audio whose length differs from the catalog's by more than the larger of the two is another recording: the next add-on plays. Both 0: no check |
| `cooldown_seconds` (30), `cooldown_errors` (3) | An add-on that answers with this many errors in a row, no more than ten minutes apart, is passed over for this long. 0 errors: off |
| `cooldown_timeouts` (2) | An add-on is passed over for `cooldown_seconds` after this many timeouts without a delivery in between, and again after each further one until it delivers |
| `slow_after` (2) | An add-on still trying when another's audio comes counts a timeout once it is past its step's time and this many times its usual time to first audio; with no usual time, once past its step's time. In the last step, which has no time of its own, the default step time (`budget_seconds`) counts as its time. Waits at its own request limits are left out. An add-on's own value comes first |
| `request_timeout_seconds` (10), `seek_timeout_seconds` (15), `download_timeout_seconds` (180) | One request to an add-on, outside a play (or for as long as its time for a download, when longer; in a play the add-on's requests for the song may take until apps stop waiting); a jump within a playing song; fetching a whole song (for playlist preparation: each transfer, from when it gets its slot) |
| `pin_ttl_seconds` (1800) | How long a song keeps its add-on and link, for seeks and repeats - also for a play under way when the song is put in the library |
| `warm_ahead_depth` (2), `warm_ahead_delay_seconds` (2), `warm_ahead_budget_seconds` (60), `warm_ahead_jobs` (2) | Warm-ahead: how many of the next songs (in the app's saved queue, else the album) are opened once a song plays, how long after its first audio, how long one may take, and how many jobs run at once for all listeners. Depth 0: off |
| `prefetch_memory_seconds` (600) | An app that fetches the next songs itself gets no warm-ahead for this long |
| `ahead_window_seconds` (0.5) | A stream that starts this soon after another song on the same app, and is not reported as playing, is a fetch ahead - except one of only two songs the app asked for together, which are both plays. 0: every stream is treated alike |
| `bulk_downloads` (4), `addon_downloads` (2) | Songs fetched at once for all scheduled downloads and playlist preparation; songs fetched per add-on or shared group. Set on Playback under Downloads and playlist preparation. 0: unlimited |
| `addon_preparations` (1) | New audio preparations at once per add-on or shared group, including sources whose cache readiness is unknown. 0: unlimited |
| `user_routings` (4), `user_downloads` (4), `user_downloads_per_hour` (0), `user_download_burst` (4) | Limits per user ([the add-on protocol](add-on-protocol.md#limits)). 0: no cap or quota off; a burst of 0: the whole hour's downloads in a row. The burst only applies when the hourly quota is enabled |
| `addon_requests_per_second` (5), `addon_request_burst` (10), `addon_audio_openings` (4) | The limits per add-on: its address and the `id` in its manifest, so add-ons served at one address count separately ([the add-on protocol](add-on-protocol.md#limits)). 0 requests a second, or 0 openings: no limit. The burst is 1 or more |
| `dash` (on) | Play add-ons' DASH links ([add-ons.md](../add-ons.md#dash-links)). Needs ffmpeg on the `PATH`. Off: the next add-on is asked |
| `dash_start` (`"at_once"`) | `"at_once"`: a DASH song plays from its first segments, served as one MP4 file of them. `"complete"`: it plays once all its segments are joined into a FLAC, M4A or MP3 file. Songs saved to the library are joined files either way |
| `dash_segments_at_once` (4) | A DASH song's segments fetched at the same time |
| `dash_joins_at_once` (3) | DASH songs fetched or joined at the same time, for all listeners together. The songs being played never wait; more of the others wait their turn |
| `dash_cache_mb` (1024) | DASH songs kept for seeks and later plays; past this size the least recently used go first. The newest is always kept |
| `dash_quality_from` (`"any"`), `dash_quality_to` (`"lossless"`) | Of the qualities a DASH manifest offers, the highest in this range: `"128"`, `"192"`, `"256"`, `"320"` (kbit/s) or `"lossless"`; the lower end may also be `"any"`, and may not be above the upper. On the Playback page |
| `keep_streamed_audio` (false) | Set as [configuration.md](../configuration.md#delivery) says. Off, a song's audio that Shijhon received in full while an app streamed it is only held for the play's seeks |

### Audio quality in detail

For a listener with a minimum quality
([choosing it](../configuration.md#audio-quality)):

- A song whose file is below it - an owned or imported file, or a catalog song's
  downloaded audio - plays from the add-ons under its own ID when they have the recording
  (found as any catalog song is: its ISRC, else its title, artist and length). An add-on
  whose audio tells a quality below the minimum too (an MP3's bitrate, say) is passed
  over for the next one, in the add-ons' usual order and time; when none has it well
  enough, the file plays. A file that meets the minimum plays as it is, with nothing
  asked. While no one has a minimum (`quality_default` is "any" and no one chose
  another), a song with its own file is Navidrome's as before, without even a credential
  check - unless a play of it from the add-ons that began earlier is under way; otherwise
  its stream checks the listener's credentials once. A play from the add-ons keeps their
  audio for its later ranges, also when the file changes meanwhile (an upgrade) or the
  minimum does, to none too; the next play from the start follows the minimum as it is
  then.
- Through the OpenSubsonic transcoding extension, Navidrome decides from the file as
  always. When the app asks for the song as it is, with no bitrate limit, and Navidrome
  answers with direct play, the stream is the add-ons' audio, in a format the app plays
  as it is by its profile (FLAC for an app that plays only MP3 is passed over). A decision
  with a bitrate limit, or one that converts, plays the file. A later range of such a
  stream keeps the add-ons' audio with the decision it began with (also after an upgrade,
  and whatever decisions the app asks for afterwards: they decide its next plays) or with
  another one Navidrome accepts for the same app profile; any other token is Navidrome's
  to answer.
- A file below it does not play for a catalog song (a placeholder an owned recording
  plays for, or a catalog song not in the library yet): the add-ons do, and the file only
  when they have nothing. A download or conversion of such a catalog song puts the
  add-ons' audio in the library instead of a copy of that file.
- Accounts that get real audio only (`[placeholders] real_audio_accounts`) always get the
  files.
- Share links and the jukebox play the file: a share link is opened by someone without an
  account, so no one's minimum applies, and the jukebox plays on the server's own speakers
  straight from the library's files.

The quality of a file is Navidrome's (its format, bitrate, sample rate and bit depth); a
file whose quality Navidrome does not know is never passed over. The add-ons' audio is
taken as good enough unless its first bytes tell otherwise (an M4A's bitrate is not read).
An upgrade of an imported song fetches the add-ons' audio as the play was, passing over
audio below the person's minimum
([music-imports.md](../music-imports.md#upgrading-your-imported-songs)).

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
| `new_releases_most_played` (25), `new_releases_artists_per_day` (150) | For people who turned [new releases](../how-it-works.md#new-releases) on: how many of their most played artists are followed besides the artists of their favorites (0: their favorites alone), and how many artists one person's daily check asks the catalog about. Both apply at once |
| `artist_lists_catalogs` (none) | More catalogs asked for the lists of `artist_lists`, after the catalog in use: installed catalog adapters by name, such as `["musicbrainz"]`. Each is built with its adapter's default settings, used for these lists only and asked at the library pass's pace; one that is not installed or cannot be built is logged at startup and passed over ([how it works](../how-it-works.md#each-artist-of-a-joined-credit)). Applies after a restart. Under Advanced settings |
| `[search] min_query_length` (3) | Shorter searches get only Navidrome's answer |
| `[search] artist_pages` (on) | Catalog releases on your library's artist pages |
| `[search] discography_max_age_days` (7) | Saved artist discographies, top songs and the lists of radios from catalog items are refreshed in the background after this |
| `[search] artist_sync_pages` (10), `artist_sync_seconds` (10) | An app that opens this many artist pages without a saved discography within this time is syncing: those pages get only the library's answer. The same numbers hold, counted apart, for top songs and for radios from catalog items without a saved list |
| `[search] guard_searches` (60), `guard_window_seconds` (20), `guard_pause_seconds` (60) | An app that sends this many different searches within the window gets only the library's answers for the pause |
| `[search] song_only_additions` (on), `song_settle_seconds` (0.15) | Catalog results for searches that ask for songs only, and how long such a search waits before the catalog is asked |
| `[search] song_burst_searches` (4), `song_burst_seconds` (0.5), `song_burst_pause_seconds` (2) | This many different song-only searches within this time are a burst, answered from the library until the app has been quiet for the pause |

With `SHIJHON_ARTIST_LISTS_REPORT=1` in Shijhon's environment, `artist_lists` logs once
a start what it would at most write, by source, and why other songs are left; nothing is
written or recorded, the credit repair asks the other catalogs and the add-on catalogs
nothing, and music imports are published as they are (what an earlier stop left half done
is still put right). It is meant for a look before the first writes; remove it to let them
run.

### `[fill]`

| Setting | What it sets |
|---|---|
| `complete_lists` (on) | A partly owned album carries its complete song count and length in album lists and search results too, before and after its fill (in your library's lists, while all its original songs are selected). Off: lists keep the counts of Navidrome or of your library selection |
| `open_budget_seconds` (3) | How long the first view of an album waits for its match; after it the view has only your songs, and the match continues in the background |
| `sync_albums` (10), `sync_seconds` (10) | An app that views this many never-matched albums within this time is syncing: those are matched in the background |
| `retry_hours` (1), `background_pause_seconds` (5) | After a failed match (then doubling); between background matches, and after each fill and each release whose track list the library pass changes |
| `pass_start_seconds` (60), `pass_interval_hours` (6), `pass_requests_per_second` (1) | The library pass starts this long after startup, looks for new albums this often, and sends the catalog this many requests a second |

### `[notifications]`

Also on the Notifications page; the settings are listed in
[configuration.md](../configuration.md#notifications). They apply at once. In the
environment, `SHIJHON_NOTIFICATIONS__URLS` holds the addresses, separated by spaces. Each
notification has 20 seconds at its addresses, and one is sent at a time. Shijhon uses
Apprise as a library: the addresses are passed to it directly, with no Apprise
configuration files, no attachments and no plugins loaded from disk, and Apprise keeps
nothing on disk. `shijhon saved` hides the addresses. An address Apprise cannot use is
refused when it is saved or read, and the message names its place in the list, not the
address.

What lasts is kept in Shijhon's database, so a restart sends nothing again. Nothing is
sent when a state ends. A state that began while no address was set is not sent later.
Imports and playlist refreshes are looked at once a minute, and several that begin
together arrive as one notification; at most 256 wait to be sent at once. An upgrade
without its snapshot is looked at when Shijhon starts, so one that
`shijhon fills-undo --apply` made is sent at the next start. Its notification names the
schemas and why, such as how much space the snapshot needed, and no path.

### `[listenbrainz]`

Also on the ListenBrainz page; the settings are listed in
[configuration.md](../configuration.md#listenbrainz), except `timeout_seconds` (4): the
longest one request to ListenBrainz may take, its wait for a turn and its answer together.
They apply at once. A radio for a person with Last.fm never waits for ListenBrainz at all.
The token is hidden by `shijhon saved`, and a token ListenBrainz refuses is said once in
the log, without the token itself. Shijhon sends
ListenBrainz at most one request a second, for radio, discovery mixes and agents
together, as ListenBrainz asks of applications. `timeout_seconds` bounds a request's wait
for its turn and its answer together; a request that cannot get its turn in time is given
up without counting as a failure, and one that gets its turn after ListenBrainz was turned
off or began to rest is not sent. It looks a song up by name at ListenBrainz Labs
(`acr-lookup`, several names in one request), then asks Labs for the recording's similar
recordings, and for an artist's similar artists, with a session-based algorithm over about
twenty years of listens. When the recording found has none, or none was found, a token
adds `/1/metadata/lookup/` at api.listenbrainz.org, and then Labs' `recording-search` is
asked once (one request; of its first ten results only one is taken: credited to the
song's lead artist, with its title without qualifiers, preferring the one naming its
featured artists, and never one whose title says live, karaoke, instrumental, cover,
tribute, remix, acoustic, demo, unplugged, a cappella, orchestral, rehearsal, reprise,
lullaby, 8-bit, re-recorded, sped up or slowed, or whose credit says karaoke, tribute,
covers, "made famous", "in the style of" or "originally performed", unless the song's own
names say it too). A refused token is not used again until it changes, and what was looked
up while it was refused is not kept; a changed token forgets what depended on the old one.
A failure, an answer that
cannot be read or a rate limit (HTTP 429, or `X-RateLimit-Remaining: 0`) rests
ListenBrainz: for a minute, or as long as `X-RateLimit-Reset-In` says, ten minutes at most.
Answers, also "not known", are kept in memory for a day, up to 4096 of each kind; a restart
forgets them. A radio list made for a person with Last.fm while ListenBrainz's answer is
not known yet is saved under its plain key; its ListenBrainz part, added in the background
(two lists at a time), is saved under the key ending in `:both`, which such a person is
shown from then on; that background work asks the catalog at background urgency and is not
started, nor goes on to the catalog, while the radio or the catalog rests after a failure
or the app syncs or is in a burst. A list made again with Last.fm alone forgets its
`:both` list, a `:both` list is not saved from an answer found while the token was refused,
and a changed token (noted in the saved mark `radio:listenbrainz:token`, so also across
restarts) forgets every radio list made with ListenBrainz. A discovery mix asks about its twelve seed songs in one lookup and then
one request or more each, for at most 30 seconds and no more than ten seconds after
Last.fm's last answer.

### `[audiomuse]`

Also on the Sound analysis page. [Sound analysis](../sound-analysis.md) says what it adds and how to
set it up; the main settings are listed in
[configuration.md](../configuration.md#audiomuse). Its token goes only to the host
(scheme, host and port) it was set up with: one in the file or the environment to the
file's or environment's `url`, one saved on the dashboard to the address it was entered
for. When the address in use is on another host, the token is not used until it is
entered again.

| Setting | What it sets |
|---|---|
| `timeout_seconds` (20) | The longest one request to it may take |
| `sync_hours` (6) | How often Shijhon reads which songs are analyzed, and their analysis (it also does once after starting, if the last time was longer ago) |

Shijhon asks AudioMuse-AI for the songs it has analyzed 1000 at a time, and for their
analysis 100 at a time with two requests at once; with the requests for playlists, at most
four are under way together.

## The dashboard and the command line in detail

- On the Library page, ticking several albums, or all a list holds under the search,
  fills them from their most likely release, keeps them or matches them again in one go:
  the albums are done one after another in the background, the page shows how far it is,
  and stopping leaves the others as they are. Such an action takes the albums as the page
  showed them: one whose match has changed since is left alone and counted, and a page
  loaded more than half an hour ago is loaded anew first.
- `shijhon matches` reads only Shijhon's database, so it also lists albums of other
  catalogs and regions, and ones no longer in the library.
- `shijhon navidrome-accounts` tries again for `--wait` seconds what goes wrong for a
  moment (Navidrome starting, refusing logins briefly); it then exits with status 1, and
  with 2 for what needs changing first. The NixOS service runs it before Shijhon.
- `shijhon carry-over --account <name>` gives the albums once: running it again for the
  same account changes nothing, and another account needs `--replace`. It needs Navidrome
  (to look the account up) and a database this Shijhon has started on, and can run while
  Shijhon runs.
