# Music-import service contract

[Import music files](../music-imports.md) describes the upload, review, import and
removal workflows. This reference covers the Python service and its storage contract.

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
