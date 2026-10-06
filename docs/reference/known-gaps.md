# Known gaps

## Playback and add-ons

- The Add-ons and Diagnostics pages check each add-on at most once a minute; the first load
  after that waits for the answers, several seconds while an add-on is slow or failing.
- A song saved to the library while it plays keeps the add-on's file for the play's seeks
  only while Shijhon keeps the song's link (`pin_ttl_seconds`, or less when the link
  expires sooner); a seek after that reads the library's file and can fail.
- With `primary_first`, a song an app fetches ahead is served only by the primary: while
  the primary is down that fetch fails, and the song starts cold when played.
- A stream keeps the add-on connections it started with until the next change of the
  add-on settings more than `pin_ttl_seconds` later, or until Shijhon stops.
- A `HEAD` of a song without a link takes one of the user's lookup turns, so an app that
  probes and then plays can wait behind its own lookups.
- Archive downloads that Navidrome does not start answering within 20 seconds fail with
  HTTP 502.
- Shares: only the first 20 placeholders of a share get their audio when its link is
  opened, so a larger share's archive can contain silent tracks.

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
- No test uses an account with restricted library access.

## API parity and security

- JSONP answers and answers to `HEAD` are Navidrome's own: a catalog item asked for as
  JSONP gets Navidrome's "not found".
- "Audio unavailable" errors carry `type: "shijhon"` and no `serverVersion`. Extended
  answers lose `ETag` and `Last-Modified`.
- `/rest/x/../stream` matches none of Shijhon's handlers but is normalized on its way to
  Navidrome: a crafted request by a signed-in user can reach a placeholder's silent file.
- An add-on's JSON answer is decompressed before its size is checked: the 1 MiB limit
  applies to the decompressed data as it arrives.
- The marks of saved values (which host a value belongs to, which may be shown) are
  unsalted hashes alongside the values. The CI workflow pins its actions to tags, not
  commits.
