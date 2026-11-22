# Configuration

Shijhon reads one TOML file (`--config`; `/config/shijhon.toml` in the image). Start from
[`packaging/shijhon.example.toml`](../packaging/shijhon.example.toml). Many settings can
also be changed on the [dashboard](#the-dashboard).

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

These are the ones most people change, with defaults in brackets. All the others are in
the [settings reference](reference/settings.md).

Agent access is on once `[agents] public_url` has Shijhon's address, set here or on the
dashboard ([agent access](agent-access.md)).

### Navidrome and the library

| Setting | What it sets |
|---|---|
| `[navidrome] url` | Navidrome's address, as Shijhon reaches it: the Compose file's Navidrome, or one you run ([deployment.md](deployment.md#in-front-of-a-navidrome-you-already-run)) |
| `[navidrome] user`, `password` or `password_file` | The service account: a Navidrome admin account used only by Shijhon |
| `[navidrome] library_id` (1), `library_path` | The library that holds your music (ID at least 1), and where Shijhon sees it on disk |
| `[navidrome] usage_export_path` | The usage export, which the cleanup reads ([deployment.md](deployment.md#the-usage-export)) |
| `[server] trusted_proxies` (loopback) | The reverse proxies whose `X-Forwarded-For` is trusted ([deployment.md](deployment.md#network-and-reverse-proxy)) |

A Navidrome reached over a Unix socket, and artist album counts that include albums on
which an artist is only a track artist, are set with
[`[navidrome] socket` and `artist_participations`](reference/settings.md#top-level-server-navidrome-placeholders).
`[[addons]]` is in [add-ons.md](add-ons.md#adding-an-add-on).

### `[music_imports]`

File uploads are off by default. Before you set `enabled = true`, create a dedicated
writable `folder` (`"_imports"`) inside the library, one Navidrome sees too. Keep it
separate from `_shijhon` and include all its files in backups. As an administrator,
complete **Collection → Import music → Finish personal library setup** before anyone
uploads. [Music imports](music-imports.md) covers formats, quotas, transfer limits and
recovery, and the import limits are listed in the
[settings reference](reference/settings.md#music-imports).

### `[delivery]`

On the dashboard's Playback page; `keep_streamed_audio`, `delivered_days` and `delivered_gb`
on its Cleanup page.

| Setting | What it sets |
|---|---|
| `budget_seconds` (9) | A new step's time in the plan: how long its add-ons try before the next step's join them ([the plan](add-ons.md#the-plan)); also an add-on's time for a download or a song prepared ahead, for one without its own |
| `ready_checks` (false), `ready_handover_seconds` (4.5) | Ask the add-ons that can tell whether they have a song ready; one that has joins a song without audio after this long (0: it goes first). Set on the Add-ons page |
| `max_wait_seconds` (30) | The longest an app waits for the first audio, all steps together: the add-ons asked keep trying until then. At least `budget_seconds`; keep it below the apps' own timeouts |
| `keep_streamed_audio` (false) | Every song Shijhon receives in full while an app streams it, or loads it ahead, replaces its silent placeholder, as downloaded audio does ([how it works](how-it-works.md#placeholders)). Shijhon reads on to the song's end while the app stays connected; a song the app left before then is not kept. Applies at once |
| `delivered_days` (30), `delivered_gb` (10) | After this many days unused, downloaded or kept audio is replaced by its silent placeholder again. Once all of it together is larger than this size, the least recently used is replaced first. 0: no age limit, or no size limit; with both 0 it is kept for good |
| `quality_default` (`"192"`) | The lowest quality listeners hear from a file unless they chose their own ([below](#audio-quality)): `"128"`, `"160"`, `"192"`, `"256"` or `"320"` (kbit/s of compressed audio), `"lossless"` or `"any"`. Applies at once |

The limits per add-on and per user are in [add-ons.md](add-ons.md#limits).

#### Audio quality

Each person chooses the lowest quality they want to hear on their **Audio quality** page
(beside Appearance): any quality; compressed audio (MP3, AAC, Opus...) of at least 128,
160, 192, 256 or 320 kbit/s, with lossless audio (FLAC, ALAC...) at any bitrate; or
lossless audio only. Every choice but "any" also asks for 44.1 kHz. Without a choice of
their own, `quality_default` applies.

A song whose file is below that minimum, whether an owned or imported file or a catalog
song's downloaded audio, plays from the add-ons when they have the recording well enough.
When they don't, the file plays. A file that meets the minimum plays as it is. Accounts
that get real audio only, share links and the jukebox always play the files. The
[detailed rules](reference/settings.md#audio-quality-in-detail) cover apps that use the
transcoding extension, catalog songs and how a quality is read.

The same page has **Upgrade my imported songs from the add-ons** (on unless the person
switches it off): after they play one of their own imported songs that went to the
add-ons this way, the add-ons' audio becomes that song's file when it meets their minimum
and is better than the file
([music-imports.md](music-imports.md#upgrading-your-imported-songs)). It never changes
another person's imports or the music folder. Their lowest quality also decides whether an
imported file goes into a catalog song it matched
([music-imports.md](music-imports.md#your-audio-for-catalog-songs)).

### `[catalog]` and `[search]`

On the dashboard's Catalog page.

| Setting | What it sets |
|---|---|
| `kind` (`none`) | `addon`: the catalog of one of your add-ons, named by `addon` ([catalogs.md](catalogs.md#the-catalog-of-an-add-on)). Or the name of an installed catalog adapter (legacy), whose own settings go in this section too |
| `twins` (`explicit`) | Which of the clean and explicit versions of an album or song are shown: `explicit`, `clean` or `both` |
| `user_releases_per_day` (0) | How many catalog releases one account that is not a Navidrome administrator may add to the library within 24 hours ([how-it-works.md](how-it-works.md#the-daily-cap-on-adding)). 0: no limit. It applies at once |
| `artist_pictures` (on) | Library artists Navidrome finds no picture for get the catalog artist's, set in Navidrome ([how-it-works.md](how-it-works.md#artist-pictures)). Off: the pictures Shijhon set are removed. Under Advanced settings |
| `artist_lists` (on) | Files whose one artist text names several artists ("A, B") get the list of those artists from a catalog, when its names are exactly that text ([how-it-works.md](how-it-works.md#each-artist-of-a-joined-credit)). Off: the files Shijhon changed, those still as it left them, are given back as they were. Under Advanced settings |
| `artist_lists_library` (off) | With `artist_lists`, also the files in your music folder, which must then be writable for Shijhon ([how-it-works.md](how-it-works.md#each-artist-of-a-joined-credit)). Off: the files Shijhon changed there, those still as it left them, are given back. Under Advanced settings |
| `new_releases_days` (30) | How long after its release date a followed artist's release leads the newest albums of the people who turned **New releases** on ([how-it-works.md](how-it-works.md#new-releases)). 0: nobody can turn it on, and nothing is checked or shown. Applies at once. Under Advanced settings |
| `[search] budget_seconds` (8) | The longest a search, the page of an artist in your library, an artist's top songs or a radio from a catalog item waits for the catalog ([catalogs.md](catalogs.md#a-catalog-that-answers-slowly)). 120 at most |
| `[search] radio` (on) | Similar songs and similar artists of songs, albums and artists that are only in the catalog, from ListenBrainz (while [`[listenbrainz]`](#listenbrainz) is on) and from Last.fm for each person who set it up on the Playlists page ([how-it-works.md](how-it-works.md#radio-from-catalog-items)). Off: Navidrome's empty answer for them. Set in the configuration file or the environment |

More catalogs for the artist lists, and a report of what the artist lists would write
before they write anything, are in the
[settings reference](reference/settings.md#catalog-and-search).

### `[fill]` and `[cleanup]`

On the dashboard's Library and Cleanup pages. [how-it-works.md](how-it-works.md) says what
filling and the cleanup do.

| Setting | What it sets |
|---|---|
| `[fill] enabled` (on) | Matching and filling of albums that are partly in your library |
| `[fill] library_pass` (`dry_run`) | The pass over the whole library: `dry_run`, `on` or `off` |
| `[fill] auto_min_songs` (3), `auto_min_share` (0.25) | The fill policy: an album is filled automatically when you have at least this many of its songs, or this share of its tracks. 1 song: every album |
| `[fill] auto_pick` (on) | When several releases fit an album, or the one that fits numbers its tracks differently from your files, the most likely is used. Off: such albums go to the review list |
| `[fill] refresh_track_lists` (on) | The library pass reads the track lists of releases already in your library again, adds tracks the catalog added since and corrects the lengths of songs that have no audio yet ([how it works](how-it-works.md#track-lists-read-again)). Off: track lists stay as they were when their releases were added. Under Advanced settings |
| `[cleanup] mode` (`off`) | `off`; `dry_run`: the daily check logs what it would take out; `on` |
| `[cleanup] unused_days` (30) | A release nobody has used is taken out this many days after it was added |
| `[cleanup] catalog_albums` (on), `fills` (on) | Which releases: catalog albums added to the library, and the songs added to partly owned albums |

### `[audiomuse]`

On the dashboard's Sound analysis page. Optional sound analysis for described playlists
([sound-analysis.md](sound-analysis.md)); these settings apply after a restart. The
account AudioMuse-AI signs in with goes in
[`[placeholders] real_audio_accounts`](reference/settings.md#top-level-server-navidrome-placeholders),
which is set in the file or the environment only.

| Setting | What it sets |
|---|---|
| `url` | AudioMuse-AI's address. Empty (the default): sound analysis is off |
| `token` | Its API token, never shown or logged. It goes only to the host it was set up with: after the address moves to another host, enter the token again ([details](reference/settings.md#audiomuse)) |
| `server` | The name or ID AudioMuse-AI gives this Shijhon among its media servers. Empty: its default server |
| `reach` (`public`) | Where it runs: `loopback` (this machine; in a container, the container itself), `private` (also your network and the Docker host) or `public` |

### `[listenbrainz]`

On the dashboard's ListenBrainz page. ListenBrainz adds similar songs and artists, the
ones its listeners play together, to radio from catalog items, discovery mixes and agents
([how it works](how-it-works.md#radio-from-catalog-items)). It is on by default and needs
no setup or account. Only artist and song names, and the MusicBrainz IDs ListenBrainz
gives for them, are sent ([privacy](privacy-and-security.md#network)). These settings
apply at once.

| Setting | What it sets |
|---|---|
| `enabled` (on) | Whether ListenBrainz is asked. Off: radio from catalog items is made with Last.fm alone, for the people who set it up, and so are discovery mixes; agents' `find_listened_together` uses Last.fm alone |
| `token` | Optional. A ListenBrainz user token, from your ListenBrainz settings page, used for everyone. With it, a song ListenBrainz's first lookup does not find is looked up again with a looser match of its names ([details](reference/settings.md#listenbrainz)). It is sent only to api.listenbrainz.org and never shown or logged |

### `[notifications]`

On the dashboard's Notifications page. Shijhon can tell you when something needs a person,
so that you don't have to open the dashboard to find out. It sends through
[Apprise](https://github.com/caronc/apprise), which reaches ntfy, Discord, Telegram,
Slack, Matrix, email, Home Assistant, a plain JSON webhook and many more services. Each
service has an address of its own, such as `ntfy://my-topic` or
`mailto://user:password@example.com`, and Apprise's documentation lists them all. These
settings apply at once.

| Setting | What it sets |
|---|---|
| `urls` | One or more Apprise addresses, separated by spaces, commas or new lines. Empty (the default): nothing is sent. The addresses hold their services' tokens, so they are kept secret: never shown or logged. An address Apprise cannot use is refused when it is saved |
| `addon_sign_in` (on) | An add-on says its operator must sign in again. The notification links to the add-on's sign-in page when the link is a plain address, and otherwise says the link is on the Add-ons page |
| `cleanup_blocked` (on) | The cleanup took nothing out because what it reads as Navidrome's records does not match the library, or the library may not be written now |
| `usage_export` (on) | The cleanup asked for a new usage export and none arrived in time |
| `import_review` (on) | A music import is checked and waits for its person's review |
| `playlist_refresh` (on) | A playlist's scheduled refresh failed, which pauses its refreshes |
| `snapshot_skipped` (on) | A start upgraded the database without the snapshot `upgrade_snapshots` asks for, because the snapshot could not be written ([upgrades](deployment.md#upgrades)). It lasts until an administrator dismisses the line the dashboard shows about it |

Each of these is sent once when it begins, and not again while it lasts. When it ends and
begins again, it is sent again, but at most once an hour about the same add-on, import or
playlist, so an add-on that keeps signing out does not flood your phone. A notification
about someone's import or playlist names their account, never the music or the playlist.
The [detailed rules](reference/settings.md#notifications) cover restarts and timing.

**Send a test notification** on the page says whether the addresses took it. When one
could not be sent, the page says so quietly and the log has a line, both without the
address.

## The command line

`--config <file>` goes before the command. In the running container, from `packaging/`:
`docker compose exec shijhon shijhon --config /config/shijhon.toml matches`.

- `shijhon serve`: runs Shijhon.
- `shijhon matches`: lists the album matches and the review list; `--all` also lists
  complete albums and those without a match. It changes nothing.
- `shijhon cleanup`: lists what the cleanup would take out now, what it keeps, and why. It
  changes nothing, and needs `usage_export_path` or `database_path`.
- `shijhon saved`: the settings saved on the dashboard, secrets hidden;
  `--clear <section>[.<key>]` removes them.
- `shijhon catalogs`: the installed catalog adapters.
- `shijhon backup`: writes a checked copy of Shijhon's database, with the configuration
  file and the service account's password file, into a folder of its own in `backups`
  beside the database (`--to <folder>` for another place), also while Shijhon runs
  ([backups](deployment.md#backups)).
- `shijhon fills-undo`: lists the automatic fills the fill policy would not make now;
  `--apply` undoes those nobody used, with Shijhon stopped
  ([advanced deployment](reference/advanced-deployment.md#undoing-automatic-fills)).
- `shijhon usage-export`: writes the usage export (the Compose file runs it as a service).
- `shijhon navidrome-accounts`: in a new Navidrome, creates Shijhon's service account
  (`[navidrome] user`, with a random password written to `password_file` when that file is
  missing) and, with `--account <name> --account-password-file <file>`, your own, both
  admins. In a Navidrome that has accounts, it signs in with that account, which must be
  an admin there, to create the service account. Accounts Navidrome has already are kept,
  with their passwords.
- `shijhon carry-over`: shows the catalog albums carried over at the upgrade to schema 24
  and who has them; `--account <name>` gives them to that Navidrome account's Shijhon
  library, and another account later needs `--replace` ([upgrades](deployment.md#upgrades)).
- `shijhon version`.

The [command details](reference/settings.md#the-dashboard-and-the-command-line-in-detail)
cover exit statuses, retries and what each command needs.

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
- **Playback**: the [`[delivery]`](#delivery) settings.
- **Catalog**: which catalog is used, and its settings.
- **Agent access**: the address AI agents connect to over MCP, and plain HTTP on a home
  network ([agent access](agent-access.md#turn-on-agent-access)).
- **Library**: the fill policy, the library pass, the **review list** (albums whose
  match needs a decision) and the albums **picked automatically**. Fill one from a release
  you choose, keep it as it is, or match it again. A search narrows the lists by title or
  artist, and several ticked albums, or all a list holds, can be done in one go in the
  background while the page shows how far it is. A filled album whose edition is wrong
  can be filled from another of its releases instead, while nobody has used the songs the
  fill added ([how it works](how-it-works.md#matching-and-filling)).
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
