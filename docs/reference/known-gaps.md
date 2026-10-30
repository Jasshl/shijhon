# Known gaps

## Playback and add-ons

- The Add-ons and Diagnostics pages check each add-on at most once a minute; the first load
  after that waits for the answers, several seconds while an add-on is slow or failing.
- A song saved to the library while it plays (downloaded, or kept from its stream) keeps
  the add-on's file for the play's seeks only while Shijhon keeps the song's link
  (`pin_ttl_seconds`, or less when the link expires sooner); a seek after that reads the
  library's file and can fail.
- A saved song Navidrome read as 0:00 (fragmented MP4 or Ogg FLAC audio saved before) is
  put in one piece in the background at least two hours after its last use - also when
  saved audio never expires. A play of it paused longer than that may fail to seek or go
  on afterwards, since the new file's bytes lie elsewhere; playing it again works (an
  answer being sent keeps reading the old file).
- Kept streams (`keep_streamed_audio`): the requests of a play are joined into one file
  only when the add-on gives the same file a strong ETag, or within one link; an answer
  whose size the add-on does not tell is kept only when it is the whole song alone, its
  end told by its framing (chunked, or HTTP/2). A kept song stays in the add-on's container
  (FLAC, M4A, MP3, Opus, Ogg Vorbis,
  Speex, WAVE, AIFF, DSF, TrueAudio, Monkey's Audio, WavPack, Musepack, WMA, Matroska);
  audio in a container whose length Navidrome does not read goes in as the same audio in
  one that tells it: raw ADTS AAC in an M4A, Ogg FLAC as a FLAC, and a fragmented MP4
  (kept or downloaded) as a DASH song's is - FLAC as a FLAC, MP3 as an MP3, anything else
  in an ordinary M4A. A Matroska song with several artists goes in as the same audio in its
  codec's own container because ffmpeg cannot write the separate tag values. If local
  tagging, stream copying or Navidrome's verification fails, the placeholder remains.
  TrueAudio keeps streaming from the add-ons where Navidrome does not
  index it (the pinned macOS build). Downloads still put only FLAC, M4A, MP3, Opus and
  Ogg Vorbis in place. A catalog song streamed before it is in the library is kept
  only when it is added within half an hour.
- With `primary_first`, a song an app fetches ahead is served only by the primary: while
  the primary is down that fetch fails, and the song starts cold when played.
- A stream keeps the add-on connections it started with until the next change of the
  add-on settings more than `pin_ttl_seconds` later, or until Shijhon stops.
- A `HEAD` of a song without a link takes one of the user's lookup turns, so an app that
  probes and then plays can wait behind its own lookups.
- Archive downloads that Navidrome does not start answering within 20 seconds fail with
  HTTP 502.
- Shares: only the first 20 placeholders of a share get their audio when its link is
  opened. An archive still containing silent placeholders is refused with HTTP 503;
  a later request can prepare the next batch.

## Library writes and the cleanup

- The cleanup stops when one release is out of step: if Navidrome's records lack the songs
  of a release that looks unused (a placeholder deleted in Navidrome's interface, say),
  nothing is taken out until that release is used or removed. The Cleanup page and the log
  name it.
- A Navidrome restarted with purging on while Shijhon waits for its scanner (up to two
  minutes) is not noticed before the scan starts.
- Paths are checked for symbolic links when they are checked, not held against a change
  afterwards. Exploiting that needs write access to the library folder.
- A use made in Navidrome directly of a release already taken out does not add it back.
- In `dry_run` mode the daily check still completes removals and add-backs that a stop
  left half done.
- Every hour Shijhon puts the placeholder back where downloaded audio is gone, but not
  with `delivered_days` and `delivered_gb` both 0, and not when more than ten files and
  more than half of all downloaded audio are missing at once (a disk that is not mounted,
  or a library restored from an older backup).
- After a stop in the middle of a file swap: an unreadable backup
  (`<placeholder folder>/.staging/backup-<song ID>.*`; the log says "a backup of its
  delivered audio cannot be read") keeps that song from being downloaded or converted
  until it is made readable or removed. Downloaded audio in no backup ("is in none of its
  backups") is put right only when the song's own placeholder is in its place.
- During a full Navidrome scan, a filled album can show the song count of one of its two
  folders, and a fill started meanwhile can fail and is tried again later.
- A stop signal that meets a restart's last moments can exit with status 75 where a stop
  would exit with 0, so a supervisor that restarts after a failure may start Shijhon
  again.

## Catalog and views

- A catalog that neither answers nor fails makes searches wait the whole
  `[search] budget_seconds` until its own timeouts end the lookups.
- Top songs are the catalog's first 10, after the ones Navidrome has. A catalog song
  shows as the library's own where the album match links it, so a wrong match shows there
  too.
- `unstar` with the catalog ID of an album not filled yet changes nothing and answers
  "ok". A library artist added to search results by name carries no favorite or rating.
- The catalog of an add-on: albums are found by their titles alone. Adding a song
  without an album link looks the album up first; one whose album is not found cannot be
  added, and adding many at once can stop part of the way. An artist opened from a song or
  an album is found by name. Under the same name, a service that keeps the manifest `id`
  of the one before is asked for the old track IDs.

## API parity and security

- JSONP answers and answers to `HEAD` apply personal library filtering, but omit catalog
  additions and complete counts before preparation
  ([library visibility](library-visibility.md#reads-covered-by-the-adapters)). A catalog
  item asked for as JSONP gets Navidrome's "not found".
- "Audio unavailable" errors carry `type: "shijhon"` and no `serverVersion`. Extended
  answers lose `ETag` and `Last-Modified`.
- An add-on's JSON answer is decompressed before its size is checked: the 1 MiB limit
  applies to the decompressed data as it arrives.
- The marks of saved values (which host a value belongs to, which may be shown) are
  unsalted hashes alongside the values.
- The CI workflow pins its actions to tags, not commits.
