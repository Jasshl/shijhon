# Music-import service contract

This reference covers the details behind the upload, review, import and removal
workflows of [Import music files](../music-imports.md), and the Python service and its
storage contract.

## Publication and recovery

All file operations are descriptor-relative and refuse symlink components. New
folders are made scanner-readable even under a restrictive umask, fsynced as
they are created, and directory bindings are checked before
and after use. A durable
placement intent commits before an atomic no-replace link makes a file visible;
file and directory fsyncs protect the transition. No existing destination is
overwritten or adopted, even when its bytes are identical. Recovery recognizes
its own publication by the recorded staging inode and complete digest/size.

Unfinished transfers restart from byte zero. Fully received files survive retries
and restarts. A restart pauses approved publication until the same immutable
owner explicitly resumes with current access. A recreated account cannot resume
another account's job. Stop prevents pending publication; already placed originals
and completed provenance remain. This service has no automatic deletion of placed
or uncertain originals. Completed private staging names remain hard links to the
same data. Music the library already has is linked rather than published again
([details](../music-imports.md#music-the-library-already-has)); a linked file's received bytes are deleted,
and its reservation goes only once that deletion is synced - also when the import was
stopped first. A copy that joins an album is recorded, with its reservation, before its
bytes are written. One a new review gives up (reviewing again, excluding its file, a
changed match, the library changing after approval), or one a stop left unfinished,
keeps that record and reservation until its private bytes are deleted and that deletion
is synced: at the next start, before the import is checked again, or when the worker is
idle.
An active upload keeps its admitted reservation through Stop until its private
unfinished attempt is unlinked and that deletion is fsynced. Restart durably
discards every interrupted unfinished attempt before marking it retryable, so a
later Stop cannot release capacity while leaving its partial bytes. Restart also
completes interrupted cleanup of already cancelled attempts. A file's
received-byte/digest commit checks the live uploading job, item and admitted
attempt in the same transaction. If receipt wins, Stop retains the full received
reservation; if Stop wins, completion refuses and discards only that unfinished
attempt. A late cancellation cannot discard an already committed received file.

Completion requires a coordinated native scan, exact caller-native path and size
verification, completed structural refresh, a second exact lookup and local byte
verification. The receipt and owner provenance commit in the same transaction.
Publication selects a deterministic batch of at most 500 approved files and 1 GiB
of original bytes per worker tick. A file above that byte budget is processed
alone, up to the configured per-file limit (2 GiB by default). Already placed or
uncertain files take priority on resume. Each batch shares one scan and one
completed structural refresh. Every file has exact caller-native evidence before
refresh and matching evidence repeated after refresh, followed by retained-byte,
root, approved-revision and current-owner checks before its atomic receipt.
After interruption or partial credit, remaining placed files reconcile together
in a fresh bounded batch; completed receipts are not published again.
Unexpected native identity reuse pauses the import and retains files. A service
account's metadata lookup cannot substitute for the importing caller's lookup.
The read-only resolver also checks every native alias already bound to an import
asset, including IDs learned during a later restore and pending placing/placed
assets whose receipt IDs are not filled yet. Different path/size/root or missing
retained inode evidence stops any caller's structural refresh before an old alias
could attach an existing owner's provenance to a different file. Completed receipts
remain immutable when exact-asset restoration learns another native alias.

## Upload checks

Audio is sent directly, with a bounded number of files transferring at once. The browser
does not read complete audio files into memory or hash their contents. The service checks
the supplied audio; file names and local timestamps are not proof of its content. Body
sizes and optional client SHA-256 assertions are independently checked against
server-counted/hash bytes.

Uploads verify current account and library access before receiving a file, once per
second while data arrives, and before accepting the complete file. A failed check
discards the unfinished attempt. Stop and storage checks run for every chunk.

File path, size and timestamp help match a reselected source, while the saved upload
remains an immutable snapshot of bytes already received. After an uncertain response, the
page checks saved status before a bounded retry. A reloaded browser has no automatic
access to local files.

## Storage layout

The service creates a marker in the imports root bound to one state installation and
library, holds an exclusive root lock, and uses private `.incoming` staging with
`.ndignore` on that same mount. Never put retained originals inside the disposable managed
folder. Jobs are bound to the library ID, the imports root's verified identity and the
imports folder name.

Asset identity remains opaque and independent of names. Copies use
`_imports/batch-<job-id>/<selected relative path>`. Relative paths must be valid UTF-8,
within 2,048 bytes and 32 components, with no absolute path, traversal, empty
component, backslash or control character. Exact duplicates, Unicode-equivalent
or case-folded names, and file/folder conflicts are explicitly refused across the
whole manifest, including earlier pages. No collision causes an overwrite or a
silent rename. Paths the pinned scanner always ignores, such as a dot-prefixed
filename or a `.git` folder, are refused before upload with `native_ignored`;
the original selected path remains in the result. Dot-folder settings beyond
these native exclusions remain Navidrome's. The current page is bounded; earlier paths
are read in 500-row cursor pages under the configured maximum job size.

Declarations reserve capacity before uploads. Actual free space and all outstanding
reservations gate admission; actual free space is checked while streaming. Stop
releases unreceived reservations; unpublished uploaded files release theirs after
deletion. Cleanup runs in saved batches and resumes after a restart; reservations are
released only after the files are gone.

## Format checks

The [format matrix](../music-imports.md#formats-and-verification) is tested with
unmodified Navidrome **0.64.2**, its default TagLib scanner and Mutagen **1.48.1** on
**Darwin ARM64** with FFmpeg/ffprobe **9.0.2**, and **Linux ARM64** with Debian
FFmpeg/ffprobe **5.1.9**. Results agree on both tested builds except for TrueAudio. These
are concrete codec/container checks, rather than a claim about every codec that fits a
suffix. The acceptance test generates invented tones, imports tagged and untagged copies,
verifies the native path/title/tags and personal asset receipt, compares the complete
native download and raw stream with the original bytes, and fully decodes both
responses. Private staged files are absent from the native index before approval.
Shorten has no standard tag case in this matrix.

The TAK evidence uses a complete invented tone encoded and verified by the author's
TAK 2.3.3 encoder and independently decoded by FFmpeg. Mutagen reads both tagged and
untagged files; stock Navidrome logs `invalid file` and skips them. Setting
`Scanner.Extractor=ffmpeg` on the tested stock binary logs that the extractor is
unavailable, falls back to TagLib and still rejects TAK. **No tested alternate
extractor setting enables TAK here.** The importer reports `native_unsupported`,
reserves no upload bytes and publishes no file or personal provenance. This is a
remaining native compatibility limitation.

TrueAudio uses Mutagen's existing read-only parser and ID3 support. Tagged and
untagged `.tta` originals complete the import, native download, raw-stream and
full-decode checks on the tested Linux build. The tested Darwin build does not
index those valid originals. Its acceptance check verifies that approved copies
stay byte-identical, the import pauses with `indexing`, no completion or personal
import credit is recorded, and Stop retains the published copies. Every completed import
still requires exact caller-authenticated native evidence; no universal TrueAudio
scanning claim is made for other native builds or platforms.

Header validation uses existing mature Mutagen parsers in a read-only subprocess.
Matroska, Shorten and TrueAudio's channel count (not exposed by Mutagen) use
ffprobe with a fixed local demuxer, capped stdin and only
the `pipe` protocol: no filename, file/network protocol or playlist loader is
available to that probe. Parser input reads are capped at 16 MiB, as is ffprobe's
single-allocation limit. Oversized metadata or an input that cannot be inspected
within those bounds is refused. Available durations must be finite, positive and
at most seven days; sample rates and channels must be valid, including DSD rates.
A missing container duration is allowed rather than excluding playable native
audio, as with Shorten.
The subprocess has a wall deadline, a CPU limit and, on Linux, a 512 MiB address-space
limit. Cancellation and deadlines kill and join its process group, including ffprobe.
These are metadata/header checks, not a full production decode or a guarantee that
every frame plays. The acceptance matrix separately performs complete decode checks.

Native coverage comes from the pinned release's
[audio MIME definitions](https://github.com/navidrome/navidrome/blob/v0.64.2/resources/mime_types.yaml),
[audio eligibility](https://github.com/navidrome/navidrome/blob/v0.64.2/model/file_types.go)
and [scanner adapter](https://github.com/navidrome/navidrome/blob/v0.64.2/adapters/gotaglib/gotaglib.go).
Parser behavior is documented by [Mutagen](https://mutagen.readthedocs.io/en/latest/api/index.html)
and [ffprobe](https://ffmpeg.org/ffprobe.html). The executable evidence is
[test_music_import_formats.py](../../tests/acceptance/test_music_import_formats.py);
[fixture generation notes](../../tests/harness/fixtures/music-import-formats/README.md)
identify the rare-format encoders, commands and hashes. No recordings are downloaded.

## Matching details

Copies of a song that joins a same-named album are made in every accepted format but
Shorten (which never needs one): with Mutagen for the formats it writes, and for Matroska
with ffmpeg as a stream copy, nothing re-encoded.

Approval links the songs already in the library before it publishes the rest. Right
before a link is made, the importing person's own read of that song must still show the
same recording on the same album; a song that left the library or became another
recording after the review gets the file published instead, as any file is. A file that
is exactly the bytes of an earlier import is linked by that import's record instead of
its tags (Navidrome may name an untagged song by its file name): the earlier import must
still have published exactly those bytes - its file must still be the one Shijhon placed
or, when another file of that size was put in its place, hold exactly those bytes
(Shijhon reads it to compare) - and the importer's own read must show the song at that
file. Other bytes of that size in its place, or something there that is not a file, stop
the import to check storage; a song shown at another file stops it to check indexing.
Neither publishes the file.

Before each publication batch, every file of it still to be published - also a copy
carrying an album's tags - is compared with the library again, as it is then; a song that
would now make a second album of its name, or a copy whose album changed, takes the
import back to review. Several imports of one person linked to the same song keep it
among that person's own imports while any of them is active.

## Audio swaps

A catalog song's copy of an import's audio is reserved before it is made and released
once it is gone. A step that has to wait (the song's downloaded audio in use, say) is
tried again later, up to ten times. Each step starts from what the song plays: audio of
the import that a stop left in place before it was recorded is recorded as the import's
(and stays reserved), also when the importer's minimum changed meanwhile, and a
reservation goes only once the song is known not to play that audio. Installation
rechecks the importing account's current access at the swap; an administrator's
restoration uses that administrator's current access.

An upgrade keeps the add-ons' audio in whatever format it comes (a FLAC stays FLAC,
nothing is converted) - any container whose tags Shijhon can write and Navidrome reads,
but not raw ADTS AAC, whose length Navidrome does not read; a fragmented MP4 or Ogg FLAC,
whose length it does not read either, comes as the same audio in an ordinary M4A (FLAC,
MP3: their own files) or a FLAC. It carries the imported file's whole metadata: tag by tag
when both formats keep the same kind of tags (ID3, Vorbis comments, MPEG-4 atoms, APEv2:
comments with their descriptions and languages, ratings, applications' private data, any
other tag under its own name), else every tag through that format's own names (lyrics,
BPM, track and disc totals, sort names, original artist and album, identifiers,
ReplayGain, comments by their descriptions), and the picture. What describes the file's
own encoding or a player's state of it (its encoder and settings, length, gapless and
volume-normalization data, a streaming timestamp) is not carried: it would be wrong for
the new audio. A song a person's later import was linked to because their earlier import
already had it is the earlier import's, and is upgraded as such. Further upgrades keep
the original receipt and all earlier audio retained and counted against storage.

The swap stays in step when stopped at any point: its storage is reserved and a recorded
intent comes first, before the upgraded file is made; right before the original's name
changes, the importer's current access and Navidrome's settings that keep a missing
file's identity are checked; the upgraded file takes the original's place (same folder
and name, its own suffix), and only when a scan and the importer's own native read show
the same song ID at the new path - with the song's details as they were - does the import
record it. Anything else puts the original back, and Navidrome must show it again (scan
and the importer's own exact read) before the intent goes; such a roll back is finished
later when it cannot be at once.

For [each artist of a joined credit](../music-imports.md#each-artist-of-a-joined-credit),
the files of a new album of the import that all get one list also get the album artists
list, all of them or none. A copy made later takes the published file's place only once
Navidrome shows the song with its ID and album, and the import's records then name the
copy, so removing and restoring the import, and an upgrade, work on it.

## Removal details

Confirming a removal checks current access, file identity, native retention policy and
fresh shared-use evidence again. Changes invalidate the review or restore an interrupted
removal. Work is bounded and saved between batches.

Published originals that Navidrome never indexed are included in removal review,
with their paths and an explicit unindexed label. They receive no invented song ID
or personal import credit. Removal requires a fresh caller-authorized native check
including missing entries and a fresh usage snapshot with no record at that exact
path. A retained missing entry is an existing native identity, not an unindexed file.
New indexing after review invalidates removal approval. Restore puts verified
retained bytes back without overwriting files or creating import credit. If a
previously unindexed original acquires a native ID through a delayed scan, Restore
verifies its exact native path/size, stable asset aliases and fresh shared-use
source. It finishes with the new ID and a visible indexing warning. Choose
**Review removal** again to check that identity and its current use before another
removal. The original import receipt and frozen review ID stay unchanged. Known
native IDs remain strict; conflicting identity, access or source evidence pauses
the operation with bytes retained, and **Retry** rechecks the current facts.

## Service integration

The public Python boundary is `shijhon.music_imports`: `MusicImports`,
`ManifestEntry(client_key, path, size, sha256="")`, `NativeFile(song_id, path, size)`,
`ImportLimits` and safe `ImportError(code, message)`.

Construct `MusicImports(store, access, *, library_id, library_path,
imports_folder="_imports", managed_folder="_shijhon", limits=ImportLimits(),
scan, native_lookup, refresh, native_song=None, artwork=None, minimum=None,
catalog=None, catalog_audio=None, write_gate=None, forget=None)`. `access` implements the existing collection Access
protocol. The callback signatures are:

```python
scan(folders: Sequence[str]) -> Awaitable[None]
native_lookup(owner_id: str, library_id: str, relative_path: str) -> Awaitable[NativeFile | None]
refresh(owner_id: str, library_id: str) -> Awaitable[None]
native_song(owner_id: str, library_id: str, song_id: str) -> Awaitable[Mapping[str, Any]]
artwork(owner_id: str, library_id: str, cover_id: str) -> Awaitable[tuple[bytes, str]]
minimum(owner_id: str) -> Awaitable[shijhon.quality.Minimum]
```

`minimum` gives the importing person's minimum quality (without it nothing is gated).
`catalog` finds and adds a low-quality album's catalog version
(`music_imports.catalog_route.CatalogRoute(matcher, commits)` in the application: the
existing album matching, then the existing commit path under the import's own
authorization, with the reviewed release's fingerprint checked at the write boundary).
Without it, such albums count as not in the catalog.

`native_song` and `artwork` (`CallerLibrary.song` and `.artwork` in the application)
read one song and one cover with the importing caller's own credentials: a song a file
is linked to is checked with them before the link commits (`NativeAbsent` from
`native_song` is verified absence: the file is published instead; any other failure
pauses the import), an album's tags for a copy are read from the file that answer names,
and an upgrade compares the song's details before and after. Without `native_song`,
matches rest on the shared index alone and no copies are made (such files are held back).

`write_gate()` raises when Navidrome's settings no longer keep a missing file's identity
(`PlaceholderWrites.refused(fresh=True)` in the application); an upgrade asks it right
before it replaces a published file. `forget(song_id)` drops the playback quality gate's
cached facts of a song whose audio an import changed (`QualityGate.forget`).

`catalog_audio` (`CatalogSongAudio(engine)`, ``music_imports/catalog_audio.py``) puts
an import's audio into the catalog songs it matched; without it, they stay plain links.
`MusicImports.upgrades` upgrades a person's own imported song
(``music_imports/upgrade.py``); the application calls it through `AfterPlay` after a play
the quality gate sent to the add-ons.

Use the shared ScanCoordinator for `scan`. The exact lookup must use current
caller credentials, a bounded native response and exact library-relative path
matching. `refresh` must await a completed caller-owned structural refresh rather
than merely queueing a throttled refresh: the review's comparison and the check before
each publication batch await it. Start `run()` in the parent lifespan,
cancel/join requests and the worker before `aclose()`, and initialize the service
once. `tick()` checks one private file or publishes/reconciles one bounded approved
batch for deterministic integration.

Construct `AssetResolver(store, *, library_id, library_path,
imports_folder="_imports", managed_folder="_shijhon")` independently of intake
enablement and pass that callable to the Collection index's trusted asset lookup.
It requires no initialize/close lifecycle and performs no lock, mkdir, chmod or
marker write. Keep it wired when uploads are disabled. The enabled service's
`asset_lookup` delegates to this same resolver.
`Storage.final_parent(name, *, create=False)` is a context manager yielding an
opened parent directory descriptor and leaf filename. The context owns and closes
the descriptor. It accepts nested batch receipts; reads
never create or repair directories. Approved publication alone uses `create=True`.
Operations on a retained final path must use this interface rather than joining
an untrusted path onto the root descriptor.

The resolver accepts at most 500 caller-native source rows and returns native song ID to
durable asset ID only for exact configured-library ledger paths, matching size,
and the retained publication inode. It can recover structural import aliases; it
never establishes personal ownership. Wrong libraries return no matches. Root
failures stop the source refresh and retain the last complete generation.

Initialization permits pending legacy setup. Import operations require an explicit
persisted personal policy; missing or legacy policy yields
`personal_setup_required`. Disabling uploads does not change read policy. The
parent owns configuration, authentication, startup, mounts and legacy setup.

The async API is `capabilities(owner_id)`, `create(owner_id, request_key)`,
`declare(owner_id, job_id, page_key, entries)`, `seal(owner_id, job_id)`,
`receive(owner_id, job_id, item_id, attempt, stream)`,
`review(owner_id, job_id, *, exclude=())`,
`distinct(owner_id, job_id, item_id, *, value=True)`,
`keep_files(owner_id, job_id, item_id, *, value=True)`, `check_again(owner_id, job_id)`,
`approve(owner_id, job_id, revision, request_key)`,
`status(owner_id, job_id, *, offset=0, limit=100, problems_only=False, overlap_only=False)`,
`listing(owner_id, *, limit=20)`, `resume(owner_id, job_id)` and
`cancel(owner_id, job_id)`. `review` queues private `checking` and returns
immediately. The single bounded worker validates one file per tick outside database
transactions, reports `checked_files`, and freezes `review` when finished. Poll
status, then require explicit approval. Restart or access failure pauses checking;
resume retains its exact exclusions and completed checks. Create, declaration pages, seal and exact approval
replays are idempotent. A changed request/page/approval key conflicts. Review
exclusions create a new revision; approval cannot expand that reviewed set.
Declaration checks that the job is still declaring inside its insertion
transaction. Stop either follows committed reservation and releases its unused
capacity, or precedes insertion and refuses it without leaving a page or items.
Problems-only pages include rejected/excluded files, nonempty warnings and held or
unsure files; overlap-only pages the files the library already had or that join one of
its albums or that the quality gate looked at (`outcome`: `linked`, `catalog_song`,
`copy`, `duplicate`, `held`, `unsure`, `low_quality`; `match` holds the reason, the
library song and both qualities, and for the quality gate `below_minimum`, `route`
(`catalog`, `unchecked`, `files`), the catalog `release` and `track`, `audio` and
`kept`; `keep_files` is the person's "add my files anyway"). Both have
exact `filtered_total` and `next_offset` over that filtered set. A copy carrying an
album's tags is its own ledger item (`derived_from` its received original, which stays
`joined`); pages show it on its original's row (`copy_state`, `copy_song_id`). Whole-job counters
remain available. Counts and the page share one database snapshot.

The dashboard must validate its session, current grant and CSRF header before
reading any bytes, then pass the raw asynchronous stream to `receive`. Do not
parse a form/multipart body first. The service separately checks immutable owner,
current grants, policy, item/attempt and capacity before advancing the stream,
and rechecks access after waits and before publication/credit. No durable record
or status JSON contains reusable credentials or server absolute paths.

## Removal integration

Custom service integrations must provide the optional typed
`ImportRemovals(..., native_absent=...)` callback to handle unindexed files; without it
that case refuses removal. The callback receives owner ID, library ID and exact
library-relative path, returning `True` only after a complete bounded native read
including missing rows, with the same current caller and library grant before and
after the read. `CallerLibrary.imported_file_absent` supplies this in the application.
An ordinary `native_lookup` returning `None` is insufficient negative evidence.
