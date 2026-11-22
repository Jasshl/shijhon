# Import a playlist

An import turns a list of tracks into a Navidrome playlist, in the same order and with its
repeated songs. It finds the songs in the library, the configured catalog and the
[catalogs of add-ons](#add-on-catalogs), with no AI, listening history or third-party
accounts involved. Open **Playlists → Import a playlist** to start. If you disconnected
your Navidrome account there, connect it again on **Playlists** first.

Enter a playlist name, then paste ordered `Artist — Title` lines or upload a playlist or
export file. In pasted lines a tab or a spaced hyphen also separates artist and title, and
blank lines are ignored. If the format of a file is not recognized, choose it on the form.

| Input | Supported entry data |
| --- | --- |
| M3U / M3U8 | Ordered file paths or file URIs; extended comments, `EXTINF` names and durations |
| PLS | Numbered `File1`, `File2`, … entries, optional titles and lengths; numeric entry order |
| XSPF | Ordered tracks, alternate locations, creator/title/album, duration, relative XML bases; native song IDs and ISRC identifiers |
| JSPF | JSON playlist tracks, locations, creator/title/album, duration and recording identifiers |
| XML ASX / WPL | Ordered ASX entries or WPL media locations, scoped bases and finite repeats |
| CSV / TSV | Recording names, ISRC, duration, file locations or native song IDs; extra export columns are ignored |
| Text | One artist/title pair per line |

An import reads a finite list of tracks. Streaming manifests, smart-playlist rules and
endless repeats are not turned into songs, and start, stop and play times on entries are
refused. A malformed row rejects the submission with its entry number, so nothing
disappears from the list without notice. Files are read as UTF-8 by default, and UTF-16,
Windows-1252 or Latin-1 can be chosen on the form. The [detailed
rules](reference/playlists.md#reading-the-input) cover format detection and what each
format accepts.

## CSV and TSV exports

Common headers such as `Artist`, `Artist Name(s)`, `Track Name`, `Name`, `Album Name`,
`Path`, `Location`, `Track URI` and `Native ID` are recognized regardless of case or
punctuation, and so are comma, tab, semicolon and pipe separators. Unused columns such as
ratings, dates and service IDs are ignored. A generic `id` column is not taken to hold
Navidrome IDs.

When headers differ, or several columns could supply the same field, open **File format,
encoding and columns** and enter the exact header for artist, title, album, version,
ISRC, duration, location or native song ID. A generic `id` column, for example, can be
mapped to native song ID there. Each row needs an artist and a title, or a location, or a
native song ID. Album and version narrow the match. Quote fields that contain commas:

```csv
artist,title,album,version,rating
Glass Orchard,First Light,Paper Garden,,5
Tideline,Harbor,Evening Rooms,Live,4
Glass Orchard,First Light,Paper Garden,,5
```

ISRC and duration are optional hints that help pick the right recording:

```csv
artist,title,album,duration_ms,isrc
Glass Orchard,First Light,Paper Garden,203500,ZZSHJ2600001
```

Columns named `duration_ms` or `Duration (ms)` hold milliseconds. Those named
`duration`, `Duration (seconds)`, `Length` or `Time` hold seconds, or `M:SS` and
`H:MM:SS`. A recording whose known ISRC differs, or whose length is more than five seconds
off, is not chosen. A value Shijhon cannot read, such as `n/a`, is ignored, and that entry
is matched by its names alone. When several columns could supply the ISRC or a duration,
such as both `Duration` and `Time`, none of them is read unless you map one on the form.

## Native paths and recording choices

Local file paths, such as those in Navidrome's own M3U exports, are matched against the
files in your library, also when the library is untagged or the playlist was made on
another machine. A shortened, conflicting or ambiguous path needs you to choose the
recording, and the review shows both the requested path and the matched library path.

Shijhon never opens supplied paths on the server and never fetches supplied URLs,
artwork or nested playlists. An entry with a remote address can still be matched by its
artist and title. A remote address alone stays unresolved. A missing local file is never
replaced by another recording with the same name without asking: choose an offered
recording or enter an accessible native song ID.

After a restart or an expired sign-in, path matching can ask you to sign in again. Sign
in, then retry the saved import.

## Add-on catalogs

An entry with an artist and a title is matched in this order:

1. Your library.
2. The configured catalog.
3. The catalogs of your add-ons, in the order of the Add-ons page: every enabled add-on
   that has a catalog of its own (a search, albums and artists).

The first source with exactly one match for the entry provides it. If a source finds
several possible recordings, the entry is marked ambiguous with that source's choices, and
later sources are not asked. The review note of a match from an add-on's catalog names the
add-on ("Found in the catalog of add-on …"). If you add or turn on an add-on with a
catalog, **Recheck** can use it without restarting Shijhon.

An add-on that is busy or does not answer in time is skipped, and the entries it could not
check say "Not found yet: add-on … did not answer. Recheck later." **Recheck** or
**Retry entries not found** asks it again.

Publishing adds the matched add-on album to your library from that add-on's catalog, so
the add-on must still be enabled then. An album found this way is added as a separate
album. It does not fill an album you partly own. Songs from a slow add-on can take a while
to start; prepare the published playlist's audio on the
[Downloads](playlists.md#prepare-a-playlists-audio) page to have it ready ahead of time.
The [detailed rules](reference/playlists.md#add-on-catalogs) cover the add-ons' limits and
time.

## Progress, review and publication

An import keeps the order and the repeated entries. It can have up to 50,000 entries and
8 MiB of text by default, and administrators can change `[playlists] import_max_entries`
and `import_max_bytes`.

**Match entries for review** creates a private review, even when every entry matches. It
adds nothing to the library and creates no playlist yet. Matching a long list takes a
while: **Reload progress** shows how many entries were checked, and later how many were
prepared, written and verified. If matching was interrupted, **Retry entries not found**
continues it.

The review shows each entry as pending, matched, unresolved or ambiguous, with the exact
recording chosen for each match. The entry pages list all rows, 100 at a time, and **Show
only entries not found** lists just the entries without a match, with their number,
original input and reason. The releases that publishing can add are listed on those pages
too. Ambiguous entries offer a choice. When there are too many choices to show, enter a
precise native song ID or import again with a more specific path.

Review the recordings and releases, then choose **Publish** (the button shows the number
of entries). A complete import publishes exactly the reviewed order and repeats. For a
partial import, tick **Publish without the entries not found**. The missing entries stay
saved with their positions for a later [recheck](#recheck-missing-entries),
and the imports list shows how many were not found. Imports do not refresh on a schedule.

Publishing can add whole releases as [placeholders](how-it-works.md#placeholders), or
missing tracks to matched albums. It
does not download audio: use **Downloads** separately to prepare it. When an
administrator has set a [daily cap on adding](how-it-works.md#the-daily-cap-on-adding),
the releases an import adds count toward yours, in the order of the import's songs. At
the cap, publication stops with that reason, keeps what it has prepared, and goes on from
there when you retry it later.

**Stop** stops further work. Entries already written and releases already added stay, and
Stop never deletes a playlist or library music. A failed or uncertain publication can be
retried with the same review, without matching again. Imports are private to your
account.

## Recheck missing entries

After publishing a partial import, choose **Recheck** (the button shows how many entries
are missing) when your library, the catalog or the add-ons with catalogs may now have
them. Recheck is manual; nothing reruns automatically. It reads the playlist as it is
now in Navidrome and starts a new private review from the saved input. Only entries that
were never published are matched again, and matching alone never changes the playlist.

Your edits in Navidrome are kept. Songs you added stay, in their order. Songs you removed
stay removed, since they are not missing entries. The name, comment and visibility are
unchanged. Each newly found song goes back near its place in the import. The review lists
each change in plain words, for example "Add Artist · Title after Artist · Title", and
the playlist's old and new length.

Choose **Update playlist** to apply the review to the same Navidrome playlist. If the
playlist changed after the review, nothing is written: press **Recheck** to review it
again. The playlist keeps its ID, name, comment, visibility and owner.

Playlists of more than 5,000 entries are updated in steps. Until the update finishes, the
playlist can contain extra copies of songs, but never fewer songs. After a stop, **Resume
update** continues the saved update, and **Retry** does so after a failure. If you edited
the playlist while the copies were in it, the update cannot continue: remove any extra
copies in Navidrome, choose **Keep playlist as it is**, then Recheck. The
[detailed rules](reference/playlists.md#rechecks-and-playlist-updates) cover where found
songs go and how an update recovers.

Finding a recording and having its audio are separate. An entry absent from the library,
the catalog and the add-ons' catalogs stays not found. An add-on without a catalog can
supply audio for a recording that was found, but it cannot help find one. Audio you get
some other way can be imported instead. Once Navidrome has indexed it, recheck the saved
missing entries.
