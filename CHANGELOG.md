# Changelog

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
