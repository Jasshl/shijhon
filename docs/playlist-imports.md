# Import a playlist

Open **Playlists → Import a playlist**. Signing in already connects your Navidrome
account; after you disconnect, connect it again on **Playlists** first.
Imports use that account's accessible library, the configured catalog and the
[catalogs of add-ons](#add-on-catalogs). AI, listening history and third-party account
connections are not used.

Enter a playlist name and paste ordered `Artist — Title` lines or upload a local
playlist/export file. A tab or spaced hyphen also separates artist and title in
pasted lines. Blank pasted lines are ignored. Choose a format explicitly if automatic
detection does not recognize the document.

Automatic detection keeps artist/title pairs as text when names contain slashes,
backslashes or dotted song titles. A tab-separated title such as `Name` or `Path`
alone does not identify a table header. Recognizable artist/title headers and
leading native-ID/path headers identify tables; playlist/export file extensions
and an explicit format choice take precedence. For an ambiguous unlabelled
two-column export with an unrelated first header, choose **CSV** or upload it
with its `.csv` or `.tsv` filename.

| Input | Supported entry data |
| --- | --- |
| M3U / M3U8 | Ordered file paths or file URIs; extended comments and `EXTINF` names |
| PLS | Numbered `File1`, `File2`, … entries, optional titles; numeric entry order |
| XSPF | Ordered tracks, alternate locations, creator/title/album, relative XML bases; `navidrome:` or `subsonic:` song identifiers |
| JSPF | JSON playlist tracks, locations and creator/title/album |
| XML ASX / WPL | Ordered ASX entries or WPL media locations, scoped bases and finite repeats |
| CSV / TSV | Recording names, file locations or native song IDs; extra export columns are ignored |
| Text | One artist/title pair per line |

These are finite track-list imports. Streaming manifests, dynamic smart-playlist
rules, indefinite repeats and timed, parallel or conditional playback instructions
are not converted into song lists. ASX start times, durations, previews and markers
are explicitly refused on entries and their alternate references. ASX `ENTRYREF`
documents must be exported as their ordered tracks; they are never treated as songs
or opened recursively. Malformed music rows reject the submission with their entry
number instead of disappearing from the imported list. This is not a claim to read
every playlist dialect or every service's export.

M3U per-entry start-time, stop-time and run-time instructions are explicitly refused
before a review is saved. Ordinary comments,
recording metadata and `EXTINF` duration hints remain accepted; a duration hint by
itself does not request a clipped recording.

Files default to UTF-8. A UTF-16 byte-order mark is detected; UTF-16, Windows-1252 and
Latin-1 can also be selected on the form. XML is parsed with the existing hardened XML
library; document types, external entities and entity expansion are refused.

## CSV and TSV exports

Common headers such as `Artist`, `Artist Name(s)`, `Track Name`, `Name`, `Album Name`,
`Path`, `Location`, `Track URI` and `Native ID` are detected without case or punctuation
sensitivity. Comma, tab, semicolon and pipe separators are detected. Unused columns
such as ratings, dates and service IDs are ignored. A generic `id` column is not
assumed to contain Navidrome IDs.

Use **File format, encoding and columns** when headers differ or several columns could
supply the same field. Enter the exact header for artist, title, album, version,
location or native song ID. For example, a generic `id` column can explicitly be
mapped to native song ID. At least artist/title, a location or a native song ID is
required for each row. Album and version can constrain metadata matching. Use quoted
fields for commas inside names:

```csv
artist,title,album,version,rating
Glass Orchard,First Light,Paper Garden,,5
Tideline,Harbor,Evening Rooms,Live,4
Glass Orchard,First Light,Paper Garden,,5
```

Only ambiguity among columns supplying an imported field needs mapping. Distinct
unused headers that normalize alike, non-Latin headers and duplicated unused columns
are ignored. Exact mapping can distinguish `Track Name` from `Track_Name`; repeated
identical headers for a selected field must be renamed in the source export.

## Native paths and recording choices

Local paths are matched against Navidrome's actual file metadata as the signed-in
caller. This supports organized, untagged libraries and stock Navidrome M3U exports.
Windows separators, file URIs and Unicode normalization are handled lexically. Matching
prefers the longest shared folder suffix; unique complete relative paths can match
across different machine roots. A shortened, conflicting or ambiguous path needs an
explicit recording choice. The review shows both the requested path and matched
library path. Native IDs are checked as the caller and against this installation's
exact library before use.

The importer never opens supplied paths on the server and never fetches supplied
URLs, artwork or nested playlists. A remote location with artist/title metadata can
be matched by that metadata; a remote address alone remains unresolved. A missing
local file is not silently replaced by another recording with the same name. Choose
an offered recording or enter an accessible native song ID explicitly.

XML locations inherit nested `xml:base` values. ASX `BASE` scopes apply to the document
or individual entry, including entries inside repeat groups; an entry override does
not change subsequent entries. Bases and relative references are resolved as text
with no URL requests or filesystem access. Remote bases remain remote, including
media URI schemes, so their relative filenames cannot select unrelated local songs.
See the [ASX BASE reference](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/wmp/base-element),
[ASX timing reference](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/wmp/starttime-element)
and [XML Base specification](https://www.w3.org/TR/xmlbase/).

Path matching reuses the original native sign-in authorization held temporarily in
memory. After a restart or expired sign-in, sign in again and retry the saved import
if it requests access. No native bearer is stored in import evidence. Previously
matched recordings remain frozen when unresolved entries are retried.

## Add-on catalogs

An entry with an artist and a title is matched in this order:

1. Your library.
2. The configured catalog.
3. The catalogs of your add-ons, in the order of the Add-ons page: every enabled add-on
   that has a catalog of its own (a search, albums and artists). An add-on that is the
   configured catalog is not asked twice (identified by its add-on name).

The library and the catalogs use the same matching rules. The first one with exactly one
match for the entry provides it. If a source finds several possible recordings, the
entry is marked ambiguous with that source's choices, and later sources are not asked.
The review note of a match from an add-on's catalog names the add-on ("Found in the
catalog of add-on …").

Add-ons are read from the current Add-ons page for every match. If you add or turn on an
add-on with a catalog, **Recheck** can use it without restarting
Shijhon. These catalogs keep name-based keys even when an add-on declares a stable
identity through the full-catalog extension. The configured catalog does not change.
Add-on catalog requests count against the add-on's [limits](add-ons.md#limits) and wait
behind requests from apps. An add-on's
own time limit (`budget_seconds`, for a slow add-on) also applies to its catalog
requests here.

An add-on that is busy (HTTP 429) or does not answer in time has not said it lacks the
song. It is skipped for the rest of that matching attempt, and the entries it could not
check say "Not found yet: add-on … did not answer. Recheck later." **Recheck** or
**Retry entries not found** asks it again. Imports and other playlist work take turns,
so a slow add-on does not hold up other people's playlists.

Publishing adds the matched add-on album to your library from that add-on's catalog.
Before publishing, Shijhon checks the album again with the add-on, so the add-on must
still be enabled. Its songs play from that add-on by the add-on's own track IDs. An
album found this way is added as a separate album. It does not fill an album you partly
own. Songs from a slow add-on can take a while to start; prepare the published
playlist's audio on the [Downloads](playlists.md#prepare-a-playlists-audio) page to have
it ready ahead of time.

## Progress, review and publication

Order and repeated entries are preserved. The default resource budget is **50,000
entries and 8 MiB of decoded text** per import. Administrators can change
`[playlists] import_max_entries` (1,000–1,000,000) and `import_max_bytes`
(1,024–67,108,864 bytes) in installation configuration. These are installation resource
budgets. Curated mixes retain their separate 100-track limit.

Matching and publication run in saved batches. Native path pages contain at most 500
songs, matching visits at most 100 input rows per worker turn (fewer when slow catalogs
use half of `[playlists] run_seconds`), and native publication adds at most 200 entries
per mutation. Preparation and final recording verification
also use bounded batches. Repeated inputs can reuse saved matching results without
losing their separate positions. **Reload progress** shows how many of the entries
being matched were checked, then the prepared, written and verified counts. A Recheck
counts only the entries it matches again. Interrupted matching may need **Retry
entries not found**.
Progress is checkpointed at matching-batch boundaries and on ordinary matching
failures, with fresh authorization and Stop checks before each new recording match
and before checkpoints.
A process termination or worker timeout can require rematching the unfinished batch
of at most 100 entries; saved matches and explicit recording choices stay frozen.

**Match entries for review** creates a private review, even when every entry matches.
It adds no library files and creates no native playlist. The review shows original
entries as pending, matched, unresolved or ambiguous, with the exact selected recording
beside each match. Use the entry-page links to review all rows, 100 at a time, or
**Show only entries not found** to list just the entries without a match, with their
number, original input and reason. Releases that publication can add are also shown in
groups of up to 100 on those pages. Ambiguous entries offer explicit choices; when the
choice list exceeds its display budget, enter a precise native song ID or supply a
more specific path in a new import.

Review the recordings and release effects, then choose **Publish** (the button shows the
number of entries). A complete import
publishes its exact reviewed order and repeats. For a partial import, explicitly
confirm how many entries are published without a match. Original rows and exclusions
remain in the saved evidence. A stale review cannot approve recordings changed by
later matching or recording choices. Resubmitting the same original form reopens the
same import; changed input requires a new form. Imports do not schedule refreshes.
Missing and ambiguous entries remain saved with their original positions, metadata and
locations; publishing without them does not discard them. The imports list shows how
many entries were not found.

## Recheck missing entries

After publishing a partial import, choose **Recheck** (the button shows how many entries
are missing) when your library, the catalog or the add-ons with catalogs may now have
them. Recheck is manual; nothing reruns automatically. It reads the playlist as it is
now in Navidrome and starts a new private review from the saved input. Only entries that were never published are
matched again. Entries still in the playlist keep their native song IDs, so replacing
a catalog does not require finding them again. Matching alone never changes the
playlist.

Your edits in Navidrome are kept. Songs you added stay, in their order; songs you
removed stay removed (they are not missing entries and are not added again); the name,
comment and visibility are unchanged. Each newly found song goes right after the
nearest earlier entry of the import that is still in the playlist; without one, before
the nearest later one; if no imported entry is left, at the end. An imported song you
moved still counts as its entry. Repeated entries stay separate. If you already added a
found library song yourself, that copy is used instead of adding a second one. The
review lists each change in plain words, for example "Add Artist · Title after
Artist · Title", and the playlist's old and new length.

Choose **Update playlist** to apply the review to the same Navidrome playlist. If the
playlist changed after the review, nothing is written: press **Recheck** to review it
again. Up to 5,000 entries, the update replaces the playlist's songs in one native
write (`createPlaylist` with the playlist's ID), which Navidrome applies as one change
and which keeps the playlist's ID, name, comment, visibility and owner. If its answer
is lost, the playlist's songs are read back: the reviewed result means it was applied;
the songs as reviewed mean it was not, and **Retry** sends the same replacement again;
anything else stops with nothing more written. Once the update has been sent, a new
name, comment or visibility you set meanwhile is kept.

Larger playlists are updated in steps, because Navidrome refuses requests with 10,000
or more parameters. The unchanged beginning stays in place; the rest of the reviewed
playlist is appended in batches of at most 200 entries, then the old copies are removed
by position. Until this finishes the playlist can contain extra copies, never fewer
songs. A lost append or removal answer is reconciled against its exact intended
result and never blindly repeated. A change to the playlist's songs between steps
stops the update; a new name, comment or visibility is yours, stays, and the update
goes on. After a
stop, **Resume update** continues the saved, reviewed update after checking the
playlist, and **Retry** does so after a failure. Recheck waits until the update is
finished, so the temporary copies are not taken for your own songs. If you edited the
playlist while copies were in it, the update cannot continue: remove any extra copies in
Navidrome, choose **Keep playlist as it is**, then Recheck. Everything then in the
playlist counts as yours.

Recording matches and audio readiness are separate. An entry absent from the library,
the catalog and the add-ons' catalogs stays not found. An add-on without a catalog
can supply audio for a known recording, but does not make unknown recordings
importable. Audio acquired by an external service can instead be imported and indexed
in Navidrome before rechecking the saved missing entries.

## Writing to Navidrome

Publication uses native song IDs, owner-authenticated native playlist calls and the
existing verified catalog preparation path. Catalog tracks can add whole releases as
placeholders or missing tracks to matched albums; publication does not download audio.
A preparation failure can leave releases added. Use **Downloads** separately to prepare
published audio.

Each native create/append/replace intent is durable before the request. The entire
saved playlist is read back after every write and its exact order, repeats and
metadata are checked. Native edits stop further publication. A lost append response
is reconciled by observing its exact intended result; the append is never blindly
repeated. If its result remains unconfirmed, retry later or stop the import. A lost
create response cannot create a second playlist. Final success requires complete
ordered native readback and bounded verification of the frozen recording identities;
for a Recheck update, the imported entries are verified and your own entries must be
exactly as reviewed, in their order. Navidrome has no conditional write API, so the
final check/write race remains.

**Stop** stops further batches. A native mutation already being sent can finish, and
previously written playlist entries and catalog additions remain. Stop never deletes a
native playlist or library music. A stopped import is not resumed by later worker
ticks. Failed or uncertain publication retries retain the review, exclusions, native
IDs and pending intent; they do not rematch or recurate. When a Recheck after a stopped
update matches a library song that update already wrote to the playlist, it uses that
copy instead of adding a second one.

Original text, normalized rows, path matches, recording choices and publication review
are private state scoped to the immutable Navidrome owner ID and installation library.
They survive restarts and are removed with that account's saved playlist connection;
native playlists and library files remain.
