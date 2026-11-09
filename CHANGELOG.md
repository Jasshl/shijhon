# Changelog

## 0.5.1

- Album pages, artist pages and searches take less work per request, so an app opening many
  at once waits less. A request asks Navidrome for an account's library access once, and
  requests at the same moment share the question. Access changes still count at once.
- A search reads fewer rows from Navidrome when the first ones hold what was asked for.
- The answer to an agent's change waits a few seconds for the result and says what to call
  next. It no longer shows the old songs as if they were the result.
- An error about too many tracks by one artist names the artists and how to allow more.
- `import_playlist` says that matching runs in the background and when its review key is
  ready.
- A live playlist reads as published after a preview of a change to it is discarded.
- Discarding a preview of added tracks gives the playlist back the tracks it had. They used
  to come back with the next addition.

## 0.5.0

- Library lists and searches stay fast as a library grows, and one app's large request no
  longer holds up the others. The artist list, album pages and searches of a library of
  12,000 songs answer in a fraction of a second instead of tens of seconds, and they no
  longer fail with "the view is being prepared" while another list is read.
- After a change to the library a list answers as it stands if its refresh takes more than
  3 seconds, with the new songs following once the refresh is done, instead of failing. A
  removed song disappears at once.
- A list that takes 3 seconds or more to read is read again from Navidrome less often than
  every minute, in proportion to how long it takes, unless the library changed.
- A refresh of the library index compares it without holding up anything else and writes
  only the songs that changed. Reported plays, playlists, the Collection page and an import's
  review do less work on a large library.
- Database schema 33: new counters and an index. It upgrades on its own at startup.
- New agent tools replace a playlist's songs under the same playlist ID, remove and
  reorder songs, publish or discard previews, create and manage discovery mixes, and
  import a pasted list with match review. Agents can list all of an account's Shijhon
  playlists; reading their songs needs the matching permission.
- **Playlists** starts with AI agents: the MCP address and the connected agents.
  Administrators set the agent address under **Server → Agent access**, and can allow
  plain HTTP on a home network. `setup.sh` asks for the address.
- Administrators can allow server AI on the machine or its network from the dashboard. In
  the Compose setup, `host.docker.internal` reaches the machine Shijhon runs on.
- **Server AI** on **Playlists** is one section, with one form for the connection.
- Use optional ISRCs and durations from playlist exports to distinguish recordings
  during import, and show the supplied hints in the review.
- The NixOS service sets up Navidrome from the system's Nixpkgs and creates Shijhon's
  service account and the configured account at the first start. Navidrome listens on a Unix socket only Shijhon can open, and counts each app apart for
  its login limits.
- The NixOS options changed: `configDirectory` is gone (use `settings` and
  `environmentFile`), `usageDirectory` is now `usageExport.directory`, and `port` is
  4533 by default (was 8765). A self-managed Navidrome needs
  `navidrome.manage = false`. The guide has the steps from the earlier service.
- On NixOS, a Navidrome that already has accounts gets Shijhon's service account through
  an admin account (`account`). The service can take over an existing `services.navidrome`
  with its data, and a self-managed Navidrome no longer needs the service account made by
  hand.
- Fixed on NixOS: a Navidrome running as another user could not read the files Shijhon
  wrote into the placeholder folder.
- The NixOS service stops at build time with a Navidrome version Shijhon is not tested
  with; `navidrome.allowUntestedVersion` tries it anyway, with a warning.
- Local AI runners for playlists can run inside the NixOS service's sandbox
  (`aiRunner`).
- `[navidrome] socket` connects Shijhon to Navidrome through a Unix socket.
- A shorter NixOS guide, with a reference page for the options and less common setups.

## 0.4.0

- Rename add-ons under their **Settings** on the **Add-ons** page. Only the name changes:
  the add-on keeps its catalog and songs, and the settings that name it follow.
- List add-ons that provide only a catalog in their own **Catalog only** section, without
  the settings that only matter for audio. Playback's primary add-on and preferred
  fallback offer only add-ons that supply audio.
- Ask add-ons for audio only when their manifest lists `stream`.
- Treat a `503` with `Retry-After` at `/stream` as "still preparing": Shijhon asks again
  within the add-on's time instead of counting an error.
- Count request limits per add-on rather than per address, by default 5 a second after
  10 at once; a 429 pauses only the add-on that sent it.
- Agent-curated playlists publish the tracks that resolve instead of failing on one
  that does not, and the request's status reports every track: what it became, or why
  it was left out, with the nearest songs found. `require_all` keeps all-or-nothing.
- When several versions of a requested track match, an agent-curated playlist takes the
  original release instead of failing.
- Add tracks to a published agent-curated playlist, in the places of left-out tracks or
  at its end, with `add_playlist_tracks` (or `POST /requests/{id}/tracks`).
- A playlist change sent while another playlist is being published answers at once
  that nothing was changed and to send it again, instead of timing out without saying
  so. Status and capabilities answer during a publication.
- Upgrading migrates the database to schema 32: take a backup first
  ([upgrades](docs/deployment.md#upgrades)).

## 0.3.0

- Use an add-on as your catalog with complete album details (disc and track numbers,
  release kinds, song and ISRC lookups and complete discographies) through an optional
  [extension of the add-on protocol](docs/reference/add-on-protocol.md#the-full-catalog-extension).
  Confirm its catalog identity on the **Catalog** page ([catalogs](docs/catalogs.md)).
- Use Mornye extensions as add-ons, including as your catalog, with the separate
  [Shijhon Mornye extension bridge](https://github.com/Jasshl/shijhon-mornye-bridge). It
  is not affiliated with Mornye.
- **Discard** on a music import now deletes the uploaded files that were not added to
  the library and frees their space; files skipped or left out of a finished import are
  deleted too. Imports stay tied to their library when import limits or paths change.
- Show stars and ratings in apps' lists at once, and keep lists fast while you play
  music.
- Keep agent connections working when an agent retries a token refresh within
  20 seconds.
- Rename the Playlists page's **Disconnect** to **Forget my playlist settings and
  history**.
- Fix issues from a review of the whole project, among them: an add-on's own headers no
  longer follow redirects to other servers; agent connections limited to fewer
  permissions cannot read private playlists; errors no longer show server paths;
  library writes never replace a file already there; clean and explicit versions stay
  apart; names such as "Feathers" are no longer split as a featured artist; a missing
  or unreadable `--config` file is refused instead of starting empty.
- Upgrading migrates the database to schema 31: take a backup first
  ([upgrades](docs/deployment.md#upgrades)).

## 0.2.1

- Speed up music uploads and library updates for large import batches.
- Make interrupted uploads easier to resume without uploading completed files again.
- Let imports request a fresh sign-in without logging out or losing uploaded files.
- Show upload progress across the whole batch, including files currently transferring,
  and update received-file counts and file-page ranges as uploads advance.

## 0.2.0

- Make playlists on the new **Playlists** page and publish them to Navidrome: discovery
  mixes from your Last.fm listening history (AI curation optional), playlists you
  describe in your own words (an AI connection is needed) and imports of existing
  playlists. Mixes and described playlists can refresh regularly
  ([guide](docs/playlists.md)).
- Import a playlist from pasted lines or a file (M3U, PLS, XSPF, JSPF, ASX/WPL, CSV/TSV,
  text). Its songs are looked up in your library, the catalog and the catalogs of enabled
  add-ons; the review shows what is missing. **Recheck** looks for missing songs again
  and, once you confirm, adds them to the same playlist, keeping your own edits
  ([guide](docs/playlist-imports.md)).
- Refreshed playlists learn from your edits in Navidrome: songs you remove stay out of
  later refreshes. The playlist's page shows what it learned and can forget it.
- Base a described playlist on playlists or songs you have ("like my Evening mix, but more
  energetic"). Connect AudioMuse-AI (optional) to give it facts about how songs sound; its
  Navidrome account can be limited to songs with real audio
  ([guide](docs/sound-analysis.md)).
- Connect agents through OAuth and MCP to request playlists, read song facts and prepare a
  playlist's audio; an optional skill is included ([agent setup](docs/agent-access.md)).
- Choose what your apps show on the new **Collection** page: **All**, the default, holds
  everything you collected; **Original imported library** your imported music, the
  albums of playlists you imported and your favorites; **Shijhon library** the catalog
  albums Shijhon added because you played, saved, favorited or listed them; or a library
  you make by hand. Albums are always complete, and each account has its own
  ([guide](docs/collection.md)).
- Import music files from the browser: upload, review, then approve. Songs the library
  already has are linked instead of added again, an album below your minimum quality can
  be added from the catalog instead, and other files go into the library with their tags
  as they are. An imported file can also give its audio to the matching song Shijhon added
  from the catalog ([guide](docs/music-imports.md)).
- Choose a minimum audio quality on your **Audio quality** page. For a song whose file is
  below it, Shijhon tries the add-ons first, passing over audio known to be below your
  minimum; otherwise the file plays. After you play one of your own imported songs this
  way, better audio from the add-ons replaces its file (on by default; switch it off on
  the same page) ([guide](docs/configuration.md#audio-quality)).
- Turn on **Keep streamed songs** on the Cleanup page to keep a song Shijhon received in
  full while an app streamed it in place of its silent placeholder, as a downloaded song
  is kept.
- Prepare a playlist's audio on the new **Downloads** page, with progress, cancel and
  retry; then use your app's download action to save it on the device. Downloads are
  spread across your add-ons, with limits on simultaneous downloads and an optional
  hourly quota.
- When a catalog release is the same album as one you own, using it adds the songs your
  album lacks into that album, keeping your files and song IDs, instead of a second album
  beside it. When your album cannot safely take them, nothing is added and the album waits
  for review in Library; albums without a match get a **Match again** action.
- Songs saved from add-ons as fragmented MP4 or Ogg FLAC no longer show as 0:00 in apps:
  they go into the library in a form whose length Navidrome reads, without re-encoding,
  and songs saved before are corrected in the background.
- A redesigned dashboard: pages grouped in a sidebar, shorter text with details on demand
  and a save bar on settings pages. Drag add-ons into order, or switch them all on or off.
- Run Shijhon as a NixOS service from the included Nix flake ([guide](docs/nixos.md)).
- Changed defaults: for compressed files below 192 kbit/s, and any file below 44.1 kHz,
  the add-ons are tried first (see the minimum quality above); set `[delivery]
  quality_default = "any"` to play every file as before. Four whole-song downloads per user
  at once (was two) and no hourly download quota (was 120); set `[delivery]
  user_downloads` and `user_downloads_per_hour` to keep the old limits.

- Shijhon now chooses an edition automatically when several fit an album you partly own.
  Automatic and manual matching can handle different track numbers.
- Review or change automatic choices on the Library page before missing tracks are added.
- Unmatched albums and those marked "Keep as is" show playable catalog editions beside them.
- The Playback page hides empty add-on defaults.

Before upgrading, stop Shijhon and back up its state, Navidrome's database and the library.
The first start upgrades Shijhon's database to schema 24 and signs dashboard sessions out.
Then sign in once to **Collection** on the dashboard and wait until your music shows: until
then, music apps get an error asking for this. Catalog albums that 0.1.0b1 added are in no
library until you add them on the Collection page (**Show → Everything on the server**).
An older version cannot open the new database: going back means restoring that backup
([upgrades](docs/deployment.md#upgrades)).

## 0.1.0b1

The first public beta.
