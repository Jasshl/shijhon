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

- For Navidrome admins only, with Navidrome's own login. Admin rights are checked again at
  most once a minute (while Navidrome cannot be asked, the last answer stands for 10
  minutes). A sign-in lasts 7 days, or 12 hours without use; changing the password does not end it.
  Sign-in attempts are limited to 5 a minute per client address (IPv6: per /64).
- It shares its origin with Navidrome's web interface. Its own cookie, CSRF tokens and a
  strict content security policy protect it, but a script running in Navidrome's web
  interface could still use a signed-in admin's session. Keep Navidrome up to date; `[server] dashboard = false`
  turns the dashboard off.

## Network

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
- **Catalog covers are served as publicly cacheable** (`Cache-Control: public`, a day).

## Logs

Logs leave out query strings and credentials, and cut any address down to its host
(add-ons appear under the names you give them). Searches are logged without their text.
Playback lines name the song, the add-on and the timings, so the log shows what was played
and when. Dashboard sign-ins and changes are logged with the admin's user name.

## What Shijhon stores

| Where | What |
|---|---|
| Its database, in `state_dir` (readable by its owner only) | The placeholders, the catalog releases they came from and which Navidrome songs they became. Album matches and saved artist discographies. The add-ons' URLs and settings, which can hold keys. Settings saved on the dashboard, secrets included, with who saved them and when. Dashboard sessions (a hash of the cookie, the user name). Records of releases the cleanup took out. When each placeholder was last served. |
| `state_dir`, alongside it | A cache of catalog covers (up to 1 GB by default), songs from DASH links (up to 1 GB; emptied at every start) and downloads under way. |
| The placeholder folder, in your library | The silent files, with a cover for catalog-only albums. Audio downloaded for a download or a conversion, for a while. For share links, copies of your own files where a placeholder stands for a recording you already have. |

Shijhon writes nowhere else in your library, and stores no per-user listening history:
Navidrome scrobbles, as for your own files.

## What the cleanup reads

The usage export: a filtered copy of Navidrome's database, made alongside Shijhon by a
process with no network access. It holds favorites, ratings and plays by item, playlist
entries, bookmarks, play queues, what is shared, each song's ID and path, and each album's
ID, name and artist. It holds no users, no user IDs, no secrets, no share links' keys and
no playlist names. Reading Navidrome's database directly instead
([advanced deployment](reference/advanced-deployment.md#reading-navidromes-database-directly))
gives Shijhon's container Navidrome's users, password data and secrets.

## What a signed-in user can learn about another

- Every user of the library sees the placeholders that exist, whoever added them.
- A `download` of a playlist is inspected with Shijhon's service account, which sees every
  user's playlists. Whether the request is refused tells the caller whether another user's
  playlist of that ID holds catalog tracks.
