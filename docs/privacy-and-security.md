# Privacy and security

## Logins

- **Navidrome checks every login.** Before Shijhon handles a catalog or audio request,
  Navidrome must accept the caller's credentials (for a share link, the link itself).
  Shijhon has no user accounts of its own.
- **An accepted login is reused for a minute** (`[server] credential_cache_seconds`): a
  password changed or a user removed in Navidrome is still accepted for that long.
- **The check is a GET**, so credentials an app sent in a POST form travel in its query
  string, between Shijhon and Navidrome only.

## The dashboard

- Sign in with Navidrome's own login. Personal pages require current immutable identity
  and access to the configured music library; installation pages additionally require a
  current administrator. Unknown identity/grants refuse work, with no stale admin grace.
  A sign-in lasts 7 days, or 12 hours without use; changing the password does not end it.
  Sign-in attempts are limited to 5 a minute per client address (IPv6: per /64).
- Library reads use a temporary Navidrome bearer held only in memory and checked fresh
  with Navidrome. An hour without a successful check, or a server restart, requires
  signing in again for those reads. The dashboard session and saved library choices stay.
- It shares its origin with Navidrome's web interface. Its own cookie, CSRF tokens and a
  strict content security policy protect it, but a script running in Navidrome's web
  interface could still use a signed-in admin's session. Keep Navidrome up to date; `[server] dashboard = false`
  turns the dashboard off.

## Network

Remote [agent access](agent-access.md) uses OAuth with explicit permissions, S256 PKCE,
short-lived access tokens and rotating refresh tokens. Personal-data reads require
their separate permissions. Native identity and library access are checked fresh;
personal operations remain bound to their immutable owner. Configuring the shared AI
service and approving its models require current administrator rights, regardless of
granted scopes; with the `settings` permission, an agent can only choose among the
approved models for its own account. An agent's `admin` permission reaches the Library's
review list and a read-only status summary, only while its account is a Navidrome
administrator, which is asked of Navidrome at every call. Credential/code hashes and reusable native
authorization are held in the private state database.
Agent model subscriptions are not delegated to Shijhon by OAuth sign-in.

- **Keep Navidrome private**: reachable only through Shijhon.
- **The connection is not encrypted** as the Compose file sets Shijhon up: it is for a
  network you trust. For access from outside, put a TLS reverse proxy in front
  ([deployment.md](deployment.md#network-and-reverse-proxy)), and keep query strings out of
  its access log: apps send credentials there.
- **Add-ons reach public addresses only**, and so do their audio links and every redirect:
  each connection's addresses are checked before connecting. `reach = "loopback"` or
  `"private"` is broad: such an add-on may lead Shijhon to any address in that range.
  Link-local addresses (such as cloud metadata services) are never allowed.
- **A catalog adapter** gets an HTTP client that reaches public addresses only.
- **AudioMuse-AI**, when set up ([sound analysis](sound-analysis.md)), is reached at the
  address you set and nowhere else; no redirect is followed. Its token is sent only to the
  host it was set up with: when the address moves to another host, by whatever means, the
  token is not used until it is entered again. `reach` works as for add-ons: `"loopback"` or `"private"` is broad, so use it
  only for a service you run.
- **Notifications**, when an address is set ([configuration](configuration.md#notifications)),
  go only to the Apprise addresses you give. The add-ons' address checks do not apply to
  them: they are your own choice, a service on this machine or your home network among
  them, and Apprise's services make their own connections (HTTP, email, MQTT and others).
  Anyone who can sign in to the dashboard as an administrator can set them.
- **Last.fm is asked for similar songs and artists** when a person who set up Last.fm on
  the Playlists page starts a radio from a song, an album or an artist that is only in
  the catalog, or opens such an artist's page
  ([how it works](how-it-works.md#radio-from-catalog-items)). A request holds an artist's
  name, or an artist's name and a song's title, and that person's Last.fm API key. It
  holds no Last.fm username and nothing about what anyone listened to. The lists made from
  the answers are saved without the person's name and are used for everyone who has set
  up Last.fm. `[search] radio = false` turns these requests off.
- **ListenBrainz is asked for songs and artists listened together**, by default and for
  everyone, without any setup: for a radio from a song, an album or an artist that is only
  in the catalog, for a catalog artist's similar artists, for discovery mixes' candidates
  and for agents' `find_listened_together`
  ([how it works](how-it-works.md#radio-from-catalog-items)). Shijhon asks
  labs.api.listenbrainz.org, and with a token also api.listenbrainz.org. A request holds
  artists' names and songs' titles, or the MusicBrainz IDs ListenBrainz gave for them, and
  nothing else: no account, no username, nothing about what anyone listened to, and no
  token except the optional installation-wide one, which goes only to
  api.listenbrainz.org. Discovery mixes send the artists and titles of the most played
  songs of a person's imported history as such names, without saying whose they are.
  Shijhon names itself and its version in each request. The answers are kept in memory for
  a day; the radio lists made from them are saved like the others, without a person's
  name. The ListenBrainz page, or `[listenbrainz] enabled = false`, turns these requests
  off; `[search] radio = false` turns off those of radio.
- **Catalog covers are served as publicly cacheable** (`Cache-Control: public`, a day).

## Logs

Logs leave out query strings and credentials, and cut any address down to its host
(add-ons appear under the names you give them). Searches are logged without their text,
and requests for similar songs and artists, and ListenBrainz's lookups, without the names
they are about.
Playback lines name the song, the add-on and the timings, so the log shows what was played
and when. Dashboard sign-ins and changes log the verified user's name; failed logins omit the
submitted name because a password may have been typed there.

## What notifications say

A notification names the state and what it is about: the add-on by the name you gave it,
with the link to its sign-in page when the add-on gave one and it is a plain address. A
link with a query string, a fragment, or a user name or password could hold a credential,
so it is left out and the notification says the link is on the Add-ons page, which shows
it. One about someone's music import or playlist refresh names their account and nothing
of the music: no titles, artists or playlist names. That is more than the dashboard shows an
administrator, whose pages list only their own imports and playlists. One about an upgrade
of the database that went on without its snapshot names the schemas and why in general
words, such as how much space the snapshot needed, and no path. A notification never
holds an address, a token or a credential, and the services it passes through see what it
says. Apprise's own log, which can print an address with its token, is never written.

## What Shijhon stores

| Where | What |
|---|---|
| Its database, in `state_dir` (readable by its owner only) | The placeholders, the catalog releases they came from and which Navidrome songs they became. Album matches, saved artist discographies and the lists radios from catalog items are made of. The add-ons' URLs and settings, which can hold keys. Settings saved on the dashboard, secrets included, with who saved them and when. Dashboard sessions (a hash of the cookie, immutable user ID and canonical name). Personal playlist connections, imported history, requests and preparation jobs. The catalog releases agents were asked to add to their accounts' libraries, with each one's outcome. Shared host AI configuration, stored separately from personal profiles. Records of releases the cleanup took out. When each placeholder was last served. Sound facts read from AudioMuse-AI, if used: per song ID, its tempo, key, energy, six scores and style labels. Last.fm tags of songs (by artist and title), fetched again after 30 days. The edits learned from refreshed playlists. Connected agents' OAuth clients, grants and tokens. For accounts that turned new releases on: the artists they follow and the releases found for them, removed when they turn it off. Music import jobs and their file lists. The states notifications report, by add-on, import or playlist ID, with when each began and when one was last sent. When an upgrade went on without its snapshot: from which schema to which, when, and why (a kind such as not enough free space, with the sizes or the system's error number), until an administrator dismisses it on the dashboard. While a daily cap on adding is set: which account (its user ID) added which catalog release and when, for accounts the cap limits. These records are kept for 24 hours; older ones are removed when the next addition is counted and at each start. |
| `state_dir`, alongside it | A cache of catalog covers (up to 1 GB by default), songs from DASH links (up to 1 GB; emptied at every start) and downloads under way. Snapshots of the database taken before upgrades (the newest of the schema last upgraded from and of the highest other schema), and backups made with `shijhon backup` until you remove them: they hold what the database holds, secrets included, readable by their owner only. |
| The placeholder folder, in your library | The silent files, with a cover for catalog-only albums. Audio downloaded for a download or a conversion, for a while. For share links, copies of your own files where a placeholder stands for a recording you already have. |

Shijhon writes nowhere else in your library, except the imports folder when
[music imports](music-imports.md) are on: it holds the files people upload. Optional
playlist discovery imports a personal history profile; playback scrobbles remain
authoritative in Navidrome.

Collection also keeps each account's library profiles and a durable record of personally
added catalog/shared music, including its first attribution time, recording metadata and
whether it came from the account's activity (a play, favorite, rating, playlist,
download, share or bookmark in an app, a playlist Shijhon published for it, audio it
prepared) or from a choice on the Collection page: activity brings the album into the
account's Shijhon library. It keeps each account's favorites as last read from Navidrome
and the songs of the playlists it imported, for its Original imported library; the songs
and albums of accepted actions still to record (until recorded); and - once, from the
upgrade to schema 24 - the catalog albums carried over and, once given, the account they
were given to (its user ID, its user name then and when). Disabling songs or archiving a
profile does not erase these choices. This is one record per song, not a per-play log.
The shared music pool reuses files and catalog data across authorized accounts, while
personal collection records remain bound to their immutable owner.

## What the cleanup reads

The usage export: a filtered copy of Navidrome's database, made alongside Shijhon by a
process with no network access. It holds favorites, ratings and plays by item, playlist
entries, bookmarks, play queues, what is shared, each song's ID and path, and each album's
ID, name and artist. It holds no users, no user IDs, no secrets, no share links' keys and
no playlist names. Reading Navidrome's database directly instead
([advanced deployment](reference/advanced-deployment.md#reading-navidromes-database-directly))
gives Shijhon's container Navidrome's users, password data and secrets.

## What a signed-in user can learn about another

- Accounts with access to the same native library can browse its files and placeholders
  through **Show → Everything on the server**, whoever added them. Their active app libraries,
  custom profiles and personal addition records are separate. Shared music is not
  private file storage.
- A `download` of a playlist is inspected with Shijhon's service account, which sees every
  user's playlists. Whether the request is refused tells the caller whether another user's
  playlist of that ID holds catalog tracks.
