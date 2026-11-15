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
- Held and kept streams (`keep_streamed_audio`): the requests of a play are joined into one
  file only when the add-on gives the same file a strong ETag, or within one link; an
  answer whose size the add-on does not tell is held only when it is the whole song alone,
  its end told by its framing (chunked, or HTTP/2). At most 16 songs
  are held at once (4 GiB with the whole songs waiting for the library), each until it has
  been unused for an hour; past that the least recently used goes, and a later seek of it
  goes to the add-on again. A kept song stays in the add-on's container
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
- Two songs an app loads at the same moment are both played from the start only when their
  requests reach Shijhon within 0.05 s of each other; further apart, the later one is a
  fetch ahead until the app reports it, which some apps do only seconds later. So is a
  song an app starts within half a second of another one. The order the requests came in
  decides which is which: a start waits up to half a second for the same app's earlier
  requests whose credentials are still being checked, and after that it is decided
  without them. Reports and saved queues belong to the account: another device's report
  of a song makes this app's fetch of it a play.
- A seek in a song Shijhon holds only in part (the app left early, it read only what the
  app asked for, the disk was too slow or full to hold it, or it was not held), once the
  song's link expired, needs a new link to the same file: the add-on the play started at
  first, then the others. A new link to a file without a strong ETag is taken only when its
  first bytes are the play's; when the play never read them (it began in the middle, after
  a restart), such a link is not taken. When none is taken, the request gets an error
  answer, which a web player reports as a media error.
- A song an app fetches ahead goes to the plan's first add-on that can be asked: when it
  cannot serve the song, the fetch is routed one add-on at a time, and one that waited out
  its turn fails, so the song starts cold when played.
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
- A filled album filled from another release: Shijhon holds back and refuses the uses of
  the old release's songs that reach it. A use made at Navidrome directly (its own
  website, another proxy) is not seen, and neither is one by an account without access to
  Shijhon's library, whose requests go to Navidrome as they come. Such a use made after
  the last look at who uses the songs lands on a song that is gone, and that stays
  possible for as long as Navidrome keeps the missing song: through the replacement,
  which can last minutes and, when its fill fails or Shijhon is stopped, until Shijhon
  has finished it, and for good after it for a song the new release does not have under
  the same number and title.
- After such a replacement, an album of yours that was a part of the old release and now
  has another title or artist than the filled album keeps saying so until it is matched
  again; albums of the same title and artist are decided anew with the replacement.
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
- A radio from a catalog item holds at most 50 songs, and a catalog artist has at most
  15 similar artists. A similar song that your library has and the catalog lacks is left
  out. An artist's and an album's radio are made of the catalog's top songs, so with a
  catalog that has none they are empty and a song's radio is not filled up. An item of an
  add-on's catalog that is not the catalog in use gets Navidrome's empty answer. A radio
  started from a song, an album or an artist in your library is Navidrome's own, with
  music from your library only.
- `unstar` with the catalog ID of an album not filled yet changes nothing and answers
  "ok". A library artist added to search results by name carries no favorite or rating.
- The catalog of an add-on: albums are found by their titles alone. Adding a song
  without an album link looks the album up first; one whose album is not found cannot be
  added, and adding many at once can stop part of the way. An artist opened from a song or
  an album is found by name. Under the same name, a service that keeps the manifest `id`
  of the one before is asked for the old track IDs.

- New releases: "most played" counts all plays of the albums an account played in the
  last 90 days, since Navidrome keeps a play count and the last play of an album, not
  each play, and it reads the 500 albums played most recently. JSONP and `HEAD` requests
  and the folder-based `getAlbumList` get Navidrome's own list, as searches get no
  additions there. An account whose saved dashboard sign-in Navidrome no longer accepts
  is not checked until it signs in again, and its saved list stays meanwhile.
- Track lists read again by the library pass: only added tracks and corrected lengths
  are applied. A changed title, artist, ISRC or explicit mark and a renumbering are
  counted and left, and a track the catalog puts where a song of the album already is, is
  not added. A new track with the title of a song the album has is not added when no
  ISRC tells them apart, also when it is another recording. Albums matched but not filled
  yet keep the track list of their match, and releases added from another catalog than
  the one in use are not read again. A release whose tracks all got new IDs at the
  catalog is left as it is.
- A catalog track asked for by a new ID the catalog gave it (a star or a playlist entry
  of it) is added to its album beside the song it already is there.

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
