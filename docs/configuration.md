# Configuration

Shijhon reads one TOML file (`--config`; `/config/shijhon.toml` in the image). Start from
[`packaging/shijhon.example.toml`](../packaging/shijhon.example.toml).

## Which value applies

Lowest first:

1. the built-in default;
2. the configuration file;
3. a value saved on the dashboard (the `[delivery]`, `[catalog]`, `[fill]`, `[cleanup]`,
   `[audiomuse]`, `[listenbrainz]`, `[agents]` and `[notifications]` settings,
   `[search] budget_seconds`,
   `[playlists] allow_private_api`, and the add-ons);
4. an environment variable. The dashboard shows such a setting locked, with the variable's
   name.

Environment variables are named `SHIJHON_<SECTION>__<KEY>`, for example
`SHIJHON_NAVIDROME__URL`; top-level keys have no section (`SHIJHON_STATE_DIR`). A list is
JSON (`SHIJHON_SERVER__TRUSTED_PROXIES='["127.0.0.0/8"]'`), and so is the add-on list
(`SHIJHON_ADDONS`). With Compose, they go under the `shijhon` service's `environment:`;
the variables in `.env` only fill in the Compose file. A value outside a setting's limits
is refused at startup, with a message that names the setting.

## Settings saved on the dashboard

- They are kept in Shijhon's database, secrets included. Saving the value the file or the
  default gives removes the saved one, so a later change to the file applies again.
- Most take effect immediately. The pages mark the ones that apply after a restart, and
  offer **Restart Shijhon** while one waits.
- `shijhon saved --clear <section>[.<key>]` removes them; after a restart the file's values
  or the defaults apply again ([more](reference/settings.md#settings-saved-on-the-dashboard)).

## The settings

The ones most people change, with defaults in brackets.
[The settings reference](reference/settings.md) has all the others.

Agent access is on once `[agents] public_url` has Shijhon's address, set here or on the
dashboard ([agent access](agent-access.md)).

### Navidrome and the library

| Setting | What it sets |
|---|---|
| `[navidrome] url` | Navidrome's address, as Shijhon reaches it: the Compose file's Navidrome, or one you run ([deployment.md](deployment.md#in-front-of-a-navidrome-you-already-run)) |
| `[navidrome] socket` | Navidrome's Unix socket (its `Address = "unix:<path>"`), an absolute path, when Shijhon reaches it there. `url` must then be an `http://` address that names no machine to reach, such as `"http://navidrome.invalid"`: it gives the `Host` header of Shijhon's own requests (apps' requests keep theirs). With `ExtAuth.TrustedSources = "@"`, Navidrome trusts only that socket |
| `[navidrome] user`, `password` or `password_file` | The service account: a Navidrome admin account used only by Shijhon |
| `[navidrome] library_id` (1), `library_path` | The library that holds your music (ID at least 1), and where Shijhon sees it on disk |
| `[navidrome] artist_participations` (`false`) | Match Navidrome's `Subsonic.ArtistParticipations` setting for artist album counts in personal library views. Set both to `true` if artists should include albums on which they perform as a track artist. Artist indexes still list album artists, as Navidrome does |
| `[navidrome] usage_export_path` | The usage export, which the cleanup reads ([deployment.md](deployment.md#the-usage-export)) |
| `[server] trusted_proxies` (loopback) | The reverse proxies whose `X-Forwarded-For` is trusted ([deployment.md](deployment.md#network-and-reverse-proxy)) |

`[[addons]]` is in [add-ons.md](add-ons.md#adding-an-add-on).

### `[music_imports]`

File uploads are off by default. Set `enabled = true` only after provisioning a
dedicated writable `folder` (`"_imports"`) inside the configured native library.
Navidrome must see the same files. Keep this folder separate from `_shijhon`,
include all its files in backups, and complete the administrator's reviewed
**Collection → Import music → Finish personal library setup** step before uploading.
Existing files stay in place; setup records which account imported them and
preserves other existing users' selections. See [music imports](music-imports.md)
for formats, quotas, transfer limits and recovery.

The import limits are in bytes, seconds or counts: `max_file_bytes`, `max_job_bytes`,
`max_job_files`, `max_owner_jobs`, `max_owner_bytes`, `max_global_bytes`,
`min_free_bytes`, `max_uploads`, `upload_seconds` and `validation_seconds`.
These settings are configured in TOML or the environment and take effect on restart.
Disabling uploads does not reset ownership or existing collection choices.

### `[delivery]`

On the dashboard's Playback page; `keep_streamed_audio`, `delivered_days` and `delivered_gb`
on its Cleanup page.

| Setting | What it sets |
|---|---|
| `budget_seconds` (9) | A new step's time in the plan: how long its add-ons try before the next step's join them ([the plan](add-ons.md#the-plan)); also an add-on's time for a download or a song prepared ahead, for one without its own |
| `ready_checks` (false), `ready_handover_seconds` (4.5) | Ask the add-ons that can tell whether they have a song ready; one that has joins a song without audio after this long (0: it goes first). Set on the Add-ons page |
| `max_wait_seconds` (30) | The longest an app waits for the first audio, all steps together: the add-ons asked keep trying until then. At least `budget_seconds`; keep it below the apps' own timeouts |
| `keep_streamed_audio` (false) | Every song whose whole audio Shijhon received while an app streamed it replaces its silent placeholder, as downloaded audio does ([how it works](how-it-works.md#placeholders)). Shijhon reads a song on to its end while the app stays connected, also one the app only loaded ahead, so this is every song an app streamed or loaded to its end; a song the app left before that is not kept. Off, that audio is only held for the play's seeks. Applies at once |
| `delivered_days` (30), `delivered_gb` (10) | After this many days unused, downloaded or kept audio is replaced by its silent placeholder again. Once all of it together is larger than this size, the least recently used is replaced first. 0: no age limit, or no size limit; with both 0 it is kept for good |
| `quality_default` (`"192"`) | The lowest quality listeners hear from a file unless they chose their own on their Audio quality page ([below](#audio-quality)): `"128"`, `"160"`, `"192"`, `"256"` or `"320"` - compressed audio (MP3, AAC, Opus...) needs at least that many kbit/s, lossless audio (FLAC, ALAC...) is accepted at any bitrate, and both need 44.1 kHz - or `"lossless"` (lossless audio only, 44.1 kHz or more) or `"any"` (no check). Applies at once |
| `dash_quality_from` (`"any"`), `dash_quality_to` (`"lossless"`) | Of the qualities a DASH manifest offers, the highest in this range: `"128"`, `"192"`, `"256"`, `"320"` (kbit/s) or `"lossless"`; the lower end may also be `"any"`, and may not be above the upper |

The limits per add-on and per user are in [add-ons.md](add-ons.md#limits).

#### Audio quality

Each person chooses the lowest quality they want to hear on their **Audio quality** page
(beside Appearance): any quality; compressed audio (MP3, AAC, Opus...) of at least 128,
160, 192, 256 or 320 kbit/s, with lossless audio (FLAC, ALAC...) at any bitrate; or
lossless audio only. Every choice but "any" also asks for 44.1 kHz. Without a choice of
their own, `quality_default` applies. For that listener:

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

The same page has **Upgrade my imported songs from the add-ons** (on unless the person
switches it off): after they play one of their own imported songs that went to the
add-ons this way, the add-ons' audio becomes that song's file when it meets their minimum
and is better than the file - fetched as the play was, passing over audio below their
minimum ([music-imports.md](music-imports.md#upgrading-your-imported-songs)).
It never changes another person's imports or the music folder. Their lowest quality also
decides whether an imported file goes into a catalog song it matched
([music-imports.md](music-imports.md#your-audio-for-catalog-songs)).

### `[catalog]` and `[search]`

On the dashboard's Catalog page.

| Setting | What it sets |
|---|---|
| `kind` (`none`) | The name of an installed catalog adapter ([catalogs.md](catalogs.md)), whose own settings go in this section too. `addon`: the catalog of one of your add-ons, named by `addon` |
| `twins` (`explicit`) | Which of the clean and explicit versions of an album or song are shown: `explicit`, `clean` or `both` |
| `user_releases_per_day` (0) | How many catalog releases one account that is not a Navidrome administrator may add to the library within 24 hours ([how-it-works.md](how-it-works.md#the-daily-cap-on-adding)). 0: no limit. It applies at once |
| `artist_pictures` (on) | Library artists Navidrome finds no picture for get the catalog artist's, set in Navidrome ([how-it-works.md](how-it-works.md#artist-pictures)). Off: the pictures Shijhon set are removed. Under Advanced settings |
| `artist_lists` (on) | Files whose one artist text names several artists ("A, B") get the list of those artists from a catalog, when its names are exactly that text ([how-it-works.md](how-it-works.md#each-artist-of-a-joined-credit)). Off: the files Shijhon changed, those still as it left them, are given back as they were. Under Advanced settings |
| `artist_lists_library` (off) | With `artist_lists`, also the files in your music folder, which must then be writable for Shijhon ([how-it-works.md](how-it-works.md#each-artist-of-a-joined-credit)). Off: the files Shijhon changed there, those still as it left them, are given back. Under Advanced settings |
| `artist_lists_catalogs` (none) | More catalogs asked for the lists of `artist_lists`, after the catalog in use: installed catalog adapters by name, such as `["musicbrainz"]`. Each is built with its adapter's default settings, used for these lists only and asked at the library pass's pace; one that is not installed or cannot be built is logged at startup and passed over ([how-it-works.md](how-it-works.md#each-artist-of-a-joined-credit)). Applies after a restart. Under Advanced settings |
| `new_releases_days` (30) | How long after its release date a followed artist's release leads the newest albums of the people who turned **New releases** on ([how-it-works.md](how-it-works.md#new-releases)). 0: nobody can turn it on, and nothing is checked or shown. Applies at once. Under Advanced settings |
| `[search] budget_seconds` (8) | The longest a search, the page of an artist in your library, an artist's top songs or a radio from a catalog item waits for the catalog ([catalogs.md](catalogs.md#a-catalog-that-answers-slowly)). 120 at most |
| `[search] radio` (on) | Similar songs and similar artists of songs, albums and artists that are only in the catalog, as ListenBrainz lists them for everyone (while [`[listenbrainz]`](#listenbrainz) is on) and Last.fm for each person who set up Last.fm on the Playlists page ([how-it-works.md](how-it-works.md#radio-from-catalog-items)). Off: Navidrome's empty answer for them, as before. Set in the configuration file or the environment |

With `SHIJHON_ARTIST_LISTS_REPORT=1` in Shijhon's environment, `artist_lists` logs once
a start what it would at most write, by source, and why other songs are left; nothing is
written or recorded, the credit repair asks the other catalogs and the add-on catalogs
nothing, and music imports are published as they are (what an earlier stop left half done
is still put right). It is meant for a look before the first writes; remove it to let them
run.

### `[fill]` and `[cleanup]`

On the dashboard's Library and Cleanup pages. [how-it-works.md](how-it-works.md) says what
filling and the cleanup do.

| Setting | What it sets |
|---|---|
| `[fill] enabled` (on) | Matching and filling of albums that are partly in your library |
| `[fill] library_pass` (`dry_run`) | The pass over the whole library: `dry_run`, `on` or `off` |
| `[fill] auto_min_songs` (3), `auto_min_share` (0.25) | The fill policy: an album is filled automatically when you have at least this many of its songs, or this share of its tracks. 1 song: every album |
| `[fill] auto_pick` (on) | When several releases fit an album, or the one that fits numbers its tracks differently from your files, the most likely is used. Off: such albums go to the review list |
| `[fill] refresh_track_lists` (on) | The library pass reads the track lists of releases already in your library again, adds tracks the catalog added since and corrects the lengths of songs that have no audio yet ([how it works](how-it-works.md#track-lists-read-again)). A dry run only counts them, and with the pass off nothing is read. Off: track lists stay as they were when their releases were added. Under Advanced settings |
| `[cleanup] mode` (`off`) | `off`; `dry_run`: the daily check logs what it would take out; `on` |
| `[cleanup] unused_days` (30) | A release nobody has used is taken out this many days after it was added |
| `[cleanup] catalog_albums` (on), `fills` (on) | Which releases: catalog albums added to the library, and the songs added to partly owned albums |

### `[audiomuse]`

On the dashboard's Sound analysis page. Optional sound analysis for described playlists
([sound-analysis.md](sound-analysis.md)); these apply after a restart. The account
AudioMuse-AI signs in with is named in
[`[placeholders] real_audio_accounts`](reference/settings.md#top-level-server-navidrome-placeholders),
which is set in the file or the environment only.

| Setting | What it sets |
|---|---|
| `url` | AudioMuse-AI's address. Empty (the default): sound analysis is off |
| `token` | Its API token, never shown or logged. It goes only to the host (scheme, host and port) it was set up with: one in the file or the environment to the file's or environment's `url`, one saved on the dashboard to the address it was entered for. When the address in use is on another host, the token is not used until it is entered again |
| `server` | The name or ID AudioMuse-AI gives this Shijhon among its media servers. Empty: its default server |
| `reach` (`public`) | Where it runs: `loopback` (this machine; in a container, the container itself), `private` (also your network and the Docker host) or `public` |
| `timeout_seconds` (20) | The longest one request to it may take |
| `sync_hours` (6) | How often Shijhon reads which songs are analyzed, and their analysis (it also does once after starting, if the last time was longer ago) |

### `[listenbrainz]`

On the dashboard's ListenBrainz page. ListenBrainz is a second source of similar songs and
artists next to Last.fm: the songs and artists its listeners play together. Radio from
catalog items, discovery mixes and agents use it
([how it works](how-it-works.md#radio-from-catalog-items)). It is on by default and needs
no setup or account. Only artist and song names, and the MusicBrainz IDs ListenBrainz
gives for them, are sent ([privacy](privacy-and-security.md#network)). These settings
apply at once.

| Setting | What it sets |
|---|---|
| `enabled` (on) | Whether ListenBrainz is asked. Off: radio from catalog items is made with Last.fm alone, for the people who set it up, and so are discovery mixes; agents' `find_listened_together` uses Last.fm alone |
| `token` | Optional. A ListenBrainz user token, from your ListenBrainz settings page, used for everyone. A song that ListenBrainz Labs' exact lookup does not find with similar songs is then looked up again with ListenBrainz's metadata lookup, which needs a token and matches names more loosely, before Labs' recording search, which needs none. It is sent only to api.listenbrainz.org, is never shown or logged, and is hidden by `shijhon saved`. A token ListenBrainz refuses is said once in the log, without itself |
| `timeout_seconds` (4) | The longest one request to ListenBrainz may take, its wait for a turn and its answer together: Shijhon asks it at most once a second, as ListenBrainz asks of applications. A radio for a person with Last.fm never waits for ListenBrainz at all |

### `[notifications]`

On the dashboard's Notifications page. Shijhon can tell you when something needs a person,
so that you don't have to open the dashboard to find out. It sends through
[Apprise](https://github.com/caronc/apprise), which reaches ntfy, Discord, Telegram,
Slack, Matrix, email, Home Assistant, a plain JSON webhook and many more services, each
with an address of its own, such as `ntfy://my-topic` or
`mailto://user:password@example.com`. Apprise's documentation lists every service and the
form of its address. These settings apply at once.

| Setting | What it sets |
|---|---|
| `urls` | One or more Apprise addresses, separated by spaces, commas or new lines. Empty (the default): nothing is sent. The addresses hold their services' tokens, so they are a secret: never shown or logged, and hidden by `shijhon saved`. An address Apprise cannot use is refused when it is saved or read, and the message names its place in the list, not the address |
| `addon_sign_in` (on) | An add-on says its operator must sign in again. The notification has the link to its page for that when the add-on gave a plain one. A link with a query string, a fragment, or a user name or password could hold a credential, so it is not sent: the notification says the link is on the Add-ons page |
| `cleanup_blocked` (on) | The cleanup took nothing out because what it reads as Navidrome's records does not match the library, or the library may not be written now |
| `usage_export` (on) | The cleanup asked for a new usage export and none arrived in time |
| `import_review` (on) | A music import is checked and waits for its person's review |
| `playlist_refresh` (on) | A playlist's scheduled refresh failed, which pauses its refreshes |
| `snapshot_skipped` (on) | A start upgraded the database without the snapshot `upgrade_snapshots` asks for, because the snapshot could not be written. The notification names the schemas and why, such as how much space the snapshot needed, and no path. It lasts until an administrator dismisses the line the dashboard shows about it ([upgrades](deployment.md#upgrades)) |

Each of these is sent once when it begins, and not again while it lasts. When it ends and
begins again, it is sent again, but at most once an hour about the same add-on, import or
playlist, so an add-on that keeps signing out does not flood your phone. What lasts is kept
in Shijhon's database, so a restart sends nothing again. Nothing is sent when one ends. A
state that began while no address was set is not sent later. Imports and playlist
refreshes are looked at once a minute, and several that begin together arrive as one
notification. An upgrade without its snapshot is looked at when Shijhon starts, so one that
`shijhon fills-undo --apply` made is sent at the next start. A notification about someone's import or playlist names their account, never
the music or the playlist.

**Send a test notification** on the page says whether the addresses took it. When one
could not be sent, the page says so quietly and the log has a line, both without the
address.

## The command line

`--config <file>` goes before the command. In the running container, from `packaging/`:
`docker compose exec shijhon shijhon --config /config/shijhon.toml matches`.

- `shijhon serve`: runs Shijhon.
- `shijhon matches`: lists the album matches and the review list; `--all` also lists
  complete albums and those without a match. It changes nothing, and reads only Shijhon's
  database, so it also lists albums of other catalogs and regions, and ones no longer in
  the library.
- `shijhon cleanup`: lists what the cleanup would take out now, what it keeps, and why. It
  changes nothing, and needs `usage_export_path` or `database_path`.
- `shijhon saved`: the settings saved on the dashboard, secrets hidden;
  `--clear <section>[.<key>]` removes them.
- `shijhon catalogs`: the installed catalog adapters.
- `shijhon backup`: writes a backup of Shijhon's own state into a folder of its own in
  `backups` beside the database (`--to <folder>` for another place), also while Shijhon
  runs: a copy of the database checked with SQLite's integrity check, the configuration
  file and the service account's password file. It says what the backup holds and what it
  does not, such as Navidrome's database and the placeholder folder
  ([backups](deployment.md#backups)). It makes no backup when one of those files cannot
  be read, and refuses to run as root on a database another user owns or when it cannot
  write to the database's folder (SQLite needs that to read a database in WAL mode).
- `shijhon fills-undo`: lists the automatic fills the fill policy would not make now;
  `--apply` undoes those nobody used, with Shijhon stopped
  ([advanced deployment](reference/advanced-deployment.md#undoing-automatic-fills)).
- `shijhon usage-export`: writes the usage export (the Compose file runs it as a service).
- `shijhon navidrome-accounts`: in a new Navidrome, creates Shijhon's service account
  (`[navidrome] user`, with a random password written to `password_file` when that file is
  missing) and, with `--account <name> --account-password-file <file>`, your own, both
  admins. In a Navidrome that has accounts, it signs in with that account, which must be
  an admin there, to create the service account. Accounts Navidrome has already are kept,
  with their passwords. What goes wrong
  for a moment (Navidrome starting, refusing logins briefly) is tried again for `--wait`
  seconds; it then exits with status 1, and with 2 for what needs changing first. The
  NixOS service runs it before Shijhon.
- `shijhon carry-over`: shows the catalog albums carried over at the upgrade to schema 24
  and who has them; `--account <name>` gives them to that Navidrome account's Shijhon
  library, once - again for the same account changes nothing, another account needs
  `--replace` ([upgrades](deployment.md#upgrades)). It needs Navidrome (to look the account
  up) and a database this Shijhon has started on, and can run while Shijhon runs.
- `shijhon version`.

## The dashboard

At `/shijhon/` on Shijhon's address, sign in with your Navidrome account. Personal
playlists, imports, audio preparation, connections and appearance belong to your account.
Installation pages require a current Navidrome administrator and library access. The pages
work without JavaScript except the music-file transfer page, which needs it for
file selection, transfer progress and resuming uploads. The sidebar groups the pages
under **Music**, for everyone, and **Server**, for administrators; on a narrow screen it
is behind **Menu**. For administrators, a small count beside **Library** gives the albums
in its review list, and one beside **Add-ons** the add-ons that need their operator to
sign in. Each settings page shows the settings most people change first, the rest under
**Advanced settings**. Times are shown in your browser's time zone.

Under Music, **Collection**, **Playlists** and **Downloads** are your own music, playlists
and audio prepared for your apps, and **New releases** follows your artists
([how it works](how-it-works.md#new-releases)). Under Server:

- **Add-ons**: add, order (by dragging), enable, disable or remove add-ons, switch them
  all on or off, and their settings.
- **Playback**: the `[delivery]` settings.
- **Catalog**: which catalog is used, and its settings.
- **Library**: the fill policy, the library pass, the **review list** (albums whose
  match needs a decision) and the albums **picked automatically**. Fill one from a release
  you choose, keep it as it is, or match it again. A search narrows the lists to the
  albums whose title or artist holds its words. Tick several albums, or all a list holds
  under the search, to fill them from their most likely release, keep them or match them
  again in one go: the albums are done one after another in the background, the page
  shows how far it is, and stopping leaves the others as they are. Such an action takes
  the albums as the page showed them: one whose match has changed since is left alone and
  counted, and a page loaded more than half an hour ago is loaded anew first. A filled
  album whose edition is wrong can be filled from another of its releases instead, while
  nobody has used the songs the fill added
  ([how it works](how-it-works.md#matching-and-filling)).
- **Cleanup**: what Shijhon added, the cleanup's settings, what its last check took out
  or would take out, and the **Saved audio** settings (keeping streamed songs, and how
  long and how much saved audio is kept). A change that lets it take out
  more first shows what the next check would take out.
- **Sound analysis**: the connection to AudioMuse-AI ([guide](sound-analysis.md)).
- **ListenBrainz**: whether ListenBrainz is asked for songs and artists listened together,
  and its optional token ([settings](#listenbrainz)).
- **Notifications**: the Apprise addresses notifications go to, what is reported, and
  **Send a test notification** ([settings](#notifications)).
- **Diagnostics**: Navidrome, the add-ons, the catalog, versions, recent errors, and
  **Restart Shijhon**.

**Appearance** and **Audio quality**, at the foot of the sidebar, set the dashboard's colors
and the lowest quality you hear ([Audio quality](#audio-quality)). **Playlists**
connects your own listening profile and AI preferences; [Discovery playlists](playlists.md)
covers AI connections, runtime limits and the optional `[playlists]` configuration.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/dashboard-library-review-dark.png">
  <img width="640" src="images/dashboard-library-review-light.png" alt="The Library page: the fill policy, the library pass's counts, an album in the review list and an album with no match">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/dashboard-diagnostics-dark.png">
  <img width="640" src="images/dashboard-diagnostics-light.png" alt="The Diagnostics page: Navidrome's status and address, and each add-on's health">
</picture>
