# Library visibility in apps and the native website

The active personal library controls OpenSubsonic library browsing, synchronization
and shuffle, and the Navidrome 0.64.2 website's native music lists. Each account has three
built-in libraries (`collection/policy.py`), each whole albums - their indexed songs,
placeholders included - by its rule, then the account's hand additions and removals in
that library, which win:

- **Original imported library**: every album with one of the account's imports (shared
  originals count for everyone until import ownership is reviewed; a song an import of
  theirs was linked to), a song of a playlist they imported, or a song or album they
  favorited in Navidrome.
- **Shijhon library**: every album with a catalog song the account's activity recorded
  (`collection_history.activity`), and - for the one account `shijhon carry-over
  --account <name>` gave them to (`collection_carryover_owner`) - the catalog albums
  carried over at the upgrade to schema 24 (`collection_carryover`,
  `collection/carryover.py`).
- **All**, the default: Original imported library and Shijhon library as the account left
  them, then All's own hand additions and removals.

A song added by hand brings its whole album into that library (a hand removal still
leaves a song out). Optional custom libraries contain hand choices only: a copy is a
snapshot of its source. Browsing or editing another selection does not activate it.

The reasons are each account's own, kept in Shijhon's database
(`collection/reasons.py`, `collection/history.py`): a `star` or `unstar` Navidrome accepts
through Shijhon counts at once; the account's favorites are also read as that account at
most every five minutes while it reads library lists - with an app request's credentials
(`getStarred2`), or with its native sign-in on the native website and the Collection page
(`/api/song` and `/api/album`, `starred=true`, at most 20,000 of each) -, the first read
waited for up to five seconds. A read is bounded as a whole (30 seconds, its credentials
kept no longer), two run at a time and at most sixteen are admitted; others are read at a
later request. An import's published songs are written with its publication.

Activity is recorded only for catalog songs, after Navidrome accepted the request:
`scrobble`, `reportPlayback` (starting or playing a song), `star`, `setRating` above 0,
`createPlaylist`, `updatePlaylist` (`songIdToAdd`), `createBookmark`, `createShare` and
`download` (when audio is sent); the native website's playlist additions; the songs of a
playlist Shijhon publishes for the account (imported or generated); the catalog songs of
a playlist whose audio the account prepares on the Downloads page; and the account's
favorites read from Navidrome that Shijhon had not seen. Up to 500 distinct songs of an
app's or the website's request are confirmed with the caller's own `getSong`; a larger
request, an app request whose confirmation failed, a preparation (in its batch's
transaction) and every favorite read anew are kept in `collection_pending` and recorded
from Shijhon's placeholder ledger beside requests (`CollectionRequests.drain`, at once and
then every minute). A set that fails is retried item by item; an item that fails waits a
minute, doubling each time up to an hour, and is due again at once when Shijhon starts or
the same action comes again. (A website addition of up to 500 songs whose confirmation
fails records nothing.) `stream`, transcode decisions, play queues, fetching ahead,
browsing, searching and album pages record nothing. A hand choice on the Collection page
is kept as a choice, never as activity.

The library index is prepared and refreshed in full with Shijhon's service account
(`[navidrome] user`, `collection/reader.py` `ServiceLibrary` and `SourceRefresh.run`), so
that apps get their lists with nobody signing in. Every 30 seconds Shijhon reads
Navidrome's scan status and the configured library's record (its last scan and counts).
A library without a complete snapshot (a first start, a new library, a Navidrome taken
over) is prepared at once, also while Navidrome's first scan runs (tried again at that
pace while the growing library refuses a read), and again as soon as that scan ends.
Once a scan has ended and the record changed, the library's songs changed since the last
refresh began are read, newest first by `updatedAt` (at most 10,000): a change only inside
the placeholder folder, or imports added or changed in the music-import folder (when
music imports are set up; both at the library's root), is Shijhon's own (placeholders
join the lists from apps' requests, below; imports refresh the index themselves) and
waits up to an hour; any other change - a song added, changed or gone missing elsewhere,
an import deleted, or counts that changed with no song changed - is read once a look saw
no new change, at least 60 seconds after the last read ended (or twice as long as that
read took, whichever is longer), and at least every five minutes while changes go on:
usually about a minute after Navidrome's scan. Each read logs its reason and duration.
Telling them apart by where the songs are, rather than by which scan found them, cannot
take a scan of someone else's music for Shijhon's; a file someone puts into Shijhon's own
folders waits for the hour. What was last seen is kept in `library-source.json` in the
state folder (for that library ID and the library's creation time), so a start reads the
library again only when it changed meanwhile (once, 30 seconds after the start, when
nothing was kept). The native website's requests only remember the caller's native
bearer, for its own reads; a dashboard sign-in and **Refresh music** refresh at once with
the service account (one asked for while a refresh runs begins right after it). A
failed preparation is tried again after 30 seconds, doubling up to ten minutes, and at
once when Navidrome answers again or a scan ends. When Navidrome refuses the service
account's credentials or its admin rights later, the last snapshot keeps answering and
the Collection page says so. The index holds structural fields only, read in pages of the configured library,
so whose reads fill it changes nothing in it: what each account sees stays decided per
request, by its own current grants and library choice. A refresh that finds nothing
changed keeps its snapshot, and no list is prepared again. After a new snapshot, lists
keep answering from the facts they were built with while their refresh runs; a list being
built when it arrives is published all the same and refreshed next.
Placeholders Shijhon writes later (commits,
fills, imports, restores) join it from apps' own requests (`collection/written.py`): a
verified library read looks, at most every ten seconds and two seconds after it is
answered, for placeholders the index lacks (one local query); when there are some, those songs are read with that request's
Subsonic credentials (`getSong`, four at a time, at most 500, within two minutes) beside
the request and added to the last complete snapshot in one transaction, in its
generation, as one recorded addition. Only songs whose placeholder - of a release not
being taken out - is still recorded when that transaction writes are added: a song whose
record went is skipped, never taken for an original. A refusal of the caller's
credentials or access, or another failure, stops the addition; only songs Navidrome says
it does not have are not asked for again (for 30 minutes). Each set of cached query facts
records the libraries its binding declares and the last addition it holds, and takes in
the recorded additions it lacks before it is read (also after a stop between the two);
every cached list is marked for a refresh beside its next request, which it keeps
answering. Nothing
is prepared again from nothing, so apps see no "indexing" answers. One addition runs at a
time and at most one a minute, so a bulk write is one addition. Provenance comes from Shijhon's placeholder records, never from Subsonic
paths; tags and roles only the native API describes come with the next full refresh.
Deliveries and reverts keep song IDs and need no addition. A catalog track remains catalog music after its placeholder receives real
audio. No selection moves files, changes native IDs, edits playlists or queues, or
deletes favorites and listening history.

## Collection rules

These less common rules add to [Your collection](../collection.md).

A playlist counts once it is published; a **Recheck** that publishes an imported
playlist again replaces the songs it counts in Original imported library. Songs you add
to the playlist yourself count as an app's playlist addition only when you add them
through Shijhon.

When Shijhon first recorded who brought music in (the upgrade to schema 24), the catalog
albums already in the library that no account's record explains - many were added before
Shijhon recorded who caused them - went into no library. The server's operator can give
them to one account's Shijhon library ([upgrades](../deployment.md#upgrades)); remove them
there if you do not want them. Otherwise **Show → Everything on the server** lists them,
to add by hand.

The Collection page reads the library index above; each account sees in it what
Navidrome lets that account access. **Refresh music** updates it at once, in bounded
background pages. Browsing continues on the last complete index if a refresh is
interrupted. Personal library choices survive metadata cleanup and adding the music back.
Original and catalog origin are tracked per song, including partly filled albums;
downloading a catalog track does not turn it into an original import.

Explicitly saved catalog music, and catalog songs an active import is linked to, keep
their library metadata from unused-release cleanup. Downloaded and kept audio (**Saved
audio** on the Cleanup page) has separate retention settings.

## Reads covered by the adapters

| Surface | Selected library behavior |
| --- | --- |
| OpenSubsonic `getAlbumList`, `getAlbumList2` | Filter before paging; return exact `X-Total-Count` and selected album counts/duration. In `getAlbumList2`, a partly owned album carries its complete counts/duration, as on the album page, while all its original songs are selected: before its fill those of the matched release, while its original songs are still the ones the match found, after it Navidrome's for the whole album, whose added songs need not be selected. With `type=newest`, an account that follows artists gets its [new releases](../how-it-works.md#new-releases) first, as catalog albums: they count before the selected albums for paging and in `X-Total-Count`, and a release in the library stays among them only while the selection hides its album. |
| `getArtists`, `getIndexes` | Hide empty library artists. |
| `getArtist` | Every native album the caller may access, independently of the active library; retain catalog discovery supplements. Partly owned albums shown complete carry their complete counts/duration, as on the album page. |
| `search2`, `search3` | Apply the selection to local matches, including empty-query synchronization; retain separate catalog discovery. `search3` album entries carry complete counts/duration as `getAlbumList2` does. A catalog song or album whose library copy the selection hides stays among the catalog additions, once, and opens or plays that library copy without adding anything. |
| `getGenres`, `getSongsByGenre`, `getRandomSongs`, `getScanStatus` | Selected genres, songs, shuffle candidates and song count. |
| `getAlbum`, album `getMusicDirectory` | Complete album tracks and matching counts/duration, independently of the active library. Matched partly owned albums also show their missing catalog tracks before preparation. Folder navigation remains selected. |
| Native `/api/song`, `/api/album`, `/api/artist`, `/api/genre`, `/api/tag` | Filter library lists before totals, paging and random selection. Explicit album details and their song pages retain the complete native album. |
| Direct song IDs, explicit native ID references, playlists, queues, favorites, scrobbles and media | Retain native access rules and contents independently of the active selection. |

JSON, XML and valid JSONP callbacks retain complete native album and artist details.
Catalog expansion and complete counts before preparation are available for JSON and XML
GET requests. HEAD and JSONP describe the native album or artist, or the selected album
list or search, without them; HEAD sends no body. Its `Content-Length` is that
representation's, so for an album list or search showing complete counts it can differ
from the JSON or XML GET's. Other HEAD reads describe their corresponding GET
representation. An unsaved item is not an unauthorized item.
Navidrome remains the authority for every user's current library grants. A person
without access to Shijhon's configured library gets ordinary upstream answers for their
other accessible native music, without catalog or collection work.

Artist indexes list selected album artists, matching Navidrome. Partial OpenSubsonic
artist album counts use `[navidrome] artist_participations`, which must match
Navidrome's `Subsonic.ArtistParticipations` setting ([configuration](../configuration.md)).
Native participant-role filters and main-credit statistics use the selected tracks.

## Accounts that get real audio only

For an account named in `[placeholders] real_audio_accounts`, the selection is not a
personal library: it is every song whose audio is in the library - original and imported
files, and placeholders whose delivered audio is in place - read with the placeholder
records in the same snapshot. The same adapters apply it, also to `getAlbum`, `getArtist`
and album `getMusicDirectory`, which then hold only those songs and count them; an album,
artist or folder without any gets Navidrome's own "not found". No complete counts are
added. Such a list waits up to 30 seconds (instead of eight) for its first query
generation before answering that it is being prepared. Answers these adapters do not
page - playlists, favorites, play queues, bookmarks, shares, similar and top songs, now
playing - leave out the other songs and the albums and artists without real audio, and
count the rest.
Any other ID (a placeholder, a catalog item) is sent to Navidrome as one it does not
have. Audio requests are Navidrome's own files, never the add-ons'. Not even another
library's `musicFolderId` sends such an account past these adapters. Sharing and the
jukebox answer as Navidrome does while they are off; a link of a share the account made
earlier that holds a song without real audio (or a stream of such a song that one of its
shares holds) gets Navidrome's own answer for a share it does not have. The native
website is not covered.

## Complete query generations

Navidrome's native filters cannot join an external per-user collection. Shijhon keeps
bounded, caller-authenticated enumeration generations in a private disposable SQLite
cache. Each generation binds the immutable caller, current grants, normalized query
and complete source-index generation. Native annotated answers are private to that
caller. Shared structural metadata never supplies another person's annotations.

The adapters intersect current permissions and the shared collection predicate before
counting and paging. Counts and rows share a SQLite read snapshot; a multi-kind search
uses one snapshot. Changing the active library or its membership reuses candidate data
and re-evaluates the predicate without fetching the library again.
Query keys retain the order of repeated values, preserving native first-value filters
and genuine multi-value parameters.

Direct album details, album directories and artist details use a fresh
caller-authenticated native answer with all tracks or albums. Opening them does not
enable songs or change a profile. Their complete counts can differ from selected
counts in REST library listings; a partly owned album has the same complete counts in
both while all its original songs are selected. Folder details still use the selection
SQL in one read snapshot.
Answers retain fresh ratings, favorites and track annotations.

Native album-list entries retain complete album metadata while selection controls
which albums appear. This keeps Navidrome's shared album cache consistent with the
album page, including when a list finishes loading after the detail response.

Navidrome 0.64.2's genre-song ordering has ties that can change with the requested
page size. Partial selected genre lists retain the complete indexed upstream order
before paging. When every candidate is selected, Shijhon returns the exact native
request's body after checking its actual song IDs in the same policy snapshot.
A search whose native answer for a kind fits in one source page (fewer than 500 entries,
or none) is that complete result: the selection applies to it directly, in the search's
one snapshot, so it waits for no background query generation and none is kept or
cancelled. Only larger results and empty-query synchronization are prepared.

Source requests use the original caller's credentials. The shared provenance scan uses
pages of at most 500; private native query transport uses at most 5,000 rows per read,
a 32 MiB body limit and two concurrent reads globally. REST query pages remain at
most 500. Scans
reject changing totals, duplicate IDs and incomplete pages. Only complete generations
are published, by an atomic pointer change. Candidate JSON is compressed on disk.
Canonical random views reuse candidate data and apply a stable seed before paging.

The default limits are two index workers, eight active or queued builds, 128 cached queries,
32 per owner, two million candidates per query, a 120-second build deadline and a
1 GiB main-plus-journal budget. A request waits up to eight seconds for its query's first
generation (a search's kinds are prepared together, within the same time), then answers
HTTP 503 with `X-Shijhon-View-State: indexing` while the build goes on; apps do not ask
again, only the native website's helper does. An account's newer
search from the same app that needs preparing cancels the build of its previous one,
unless a request from another app waits for it too; one answered directly lets that build
finish for the requests waiting for it, since a burst's lookups can each be needed. A
search's build that no request waits for stops once its app has searched for something
else. Each upstream page has a 32 MiB body limit. The cache
uses a private directory (0700) and database (0600). Credentials are neither persisted in
this cache nor logged. A completed generation with unchanged identity, grants and
source generation remains readable during its bounded background refresh. Every user
request still revalidates current access. Cache capacity or source failures produce an
explicit error, never an incomplete success or an unfiltered answer.

## Native browser integration

Shijhon serves its own small same-origin transport helper and attaches it only to the
native app's HTML. Navidrome's binary, source, bundled JavaScript, CSP and authoritative
state remain unmodified. Other assets and routes pass through normally.

The helper retries only GET/HEAD music reads whose response is exactly HTTP 503 with
`X-Shijhon-View-State: indexing`. It preserves the original request, credentials,
headers and abort signal, including cancellation during retry timers. Retries honor a
bounded `Retry-After`, stop after 30 seconds or 32 attempts, and return the final error
normally. Writes, authentication, audio, dashboard/OAuth requests, cross-origin calls
and unrelated failures are never retried. Common native browsing queries prewarm after
a verified caller becomes available. A first native deep link can register that
caller's transient bearer and start a missing source index without a second account.

On a fresh installation or upgrade, Shijhon prepares the initial source index with its
service account as it starts; nobody needs to sign in. An existing complete source
survives restarts. Until the first one is complete, library lists answer HTTP 503 with
the indexing marker ("indexing, retry shortly"). Only when the service account cannot
prepare it - Navidrome refuses its sign-in, it is no admin, or it may not read the
configured library - does a library list answer HTTP 503 without the marker, with a
message that says which, and the Collection page says the same. Album and artist pages
answer from Navidrome meanwhile. Import provenance comes from Shijhon's own records,
never from the account that read the index or from REST file paths.

For a caller with several physical-library grants, collection policy applies only to
the configured Shijhon library. Other authorized music remains in combined totals and
pages. A private, complete caller-native structural scan supplies those other-library
relationships without changing the shared source index. Explicit other-library-only
queries pass through. `musicFolderId` counts only where Navidrome reads it - album
lists, shuffle, genre songs, favorites, artist indexes and searches - and as Navidrome
reads it, as a number ("01" is library 1; a value that is no number names none).
Streams, downloads and pages ignore it, as Navidrome does: a placeholder's stream with
another library's folder is still a placeholder's stream. REST combined browsing can reuse that private complete scan;
if it is missing and the caller's native bearer is not at hand (an app's request), it is
read with the service account: only the other libraries Navidrome grants that caller now,
each song checked to be in one of them. Those structural relations are shared by the
accounts that read the same libraries, each request intersecting them with its caller's
current grants. They are read again in the background after a new snapshot of the
configured library and once they are five minutes old, by one build for all those
accounts (after a failure not again for a minute, doubling up to ten); reads go on with
the last complete ones meanwhile, and lists keep their identity across snapshots. Their
source pages keep only the structural fields the relations use.
