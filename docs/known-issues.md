# Known issues

## Before you install

- **Placeholders are real files.** Every user of the library sees the ones that exist, and
  so do other programs that read your music folder
  ([hiding them](reference/advanced-deployment.md#hiding-placeholders-from-other-programs)).
  An app that talks to Navidrome directly plays their silence.
- **Navidrome's `Scanner.PurgeMissing` must stay `never`**, the default, or Shijhon
  writes nothing into the library ([why](deployment.md#what-shijhon-needs-from-navidrome)).
- **Any user can add releases**, and nothing limits how many.
- **Catalog results appear in the API's ID3 methods** (`search3`, `getArtist`,
  `getAlbum`, `getAlbumList2`, `getStarred2`). Apps that browse by folders, and
  Navidrome's own API (its web interface), show your library without them.
- **Apps that sync the library and search on the device** show no catalog results.
- **One library, no `BaseURL`, and Navidrome 0.64.2**, the only version Shijhon is
  tested with.
- **A Linux host.** Docker Desktop on macOS and Windows is for a first look only, with
  the databases in Docker volumes, the default: in a folder of the Mac or Windows disk,
  Navidrome's database can be corrupted within minutes
  ([more](deployment.md#docker-desktop-on-macos-and-windows)).
- **The dashboard shows times in UTC** when Shijhon runs in Docker.

## Playback and downloads

- **HLS links do not play.** The next add-on is asked.
- **The minimum audio quality covers streams, downloads and direct play through the
  transcoding extension.** A song whose file is below the listener's minimum still plays
  its file when the app asks for a conversion the add-ons' audio would need, and through
  the OpenSubsonic transcoding extension when the app's decision has a bitrate limit or
  converts, or its profile does not name the add-ons' format (an M4A's codec is not read:
  the app must play AAC and ALAC). After a restart, until the app asks for a new decision,
  its earlier direct-play decisions play the file. Such a song from the add-ons starts as
  late as a catalog song does. The add-ons' audio counts as good enough unless its first
  bytes tell a lower quality: an M4A's bitrate is not read.
- **An imported song is upgraded only after a plain play by its importer** (a stream, or a
  transcode stream at original quality). A play that needs a conversion, a download, the
  jukebox or a share link, or a play by someone else, does not upgrade it. The add-ons'
  audio replaces the file only when its format can carry the file's tags (any Shijhon
  writes and Navidrome reads, not raw AAC), the imported file is not Matroska or Shorten,
  and Navidrome keeps the song's identity and details for it (a recording date only an
  MPEG-4 or WMA file cannot hold is such a detail); otherwise the file stays as it is.
- **A catalog song playing an import's audio whose file goes missing** (a restore from a
  backup that leaves the placeholder folder out) plays from the add-ons again until the
  import's audio is put back: within a minute, or at the next import activity.
- **An import's audio for a catalog song only in the swap's formats.** Imports in other
  formats (WAVE, AIFF, WMA, APE and the like) linked to a catalog song stay plain links:
  the song keeps playing from the add-ons.
- **A WAVE file's RIFF INFO tags are not read.** The review reads its ID3 tags only;
  a WAVE file tagged only in RIFF INFO counts as having no album name.
- **A stream that has to be converted starts late.** When Navidrome would convert the
  audio - another format, or a lower bitrate than the add-on's audio has - Shijhon fetches
  the whole file first. If the fetch fails, the song plays in the add-on's own format.
- **Apps are told the song is FLAC.** Until a song's audio is in the library, Navidrome
  describes it as its placeholder, a FLAC file. The audio an app gets is the add-on's.
- **Archive downloads** (a whole album, artist or playlist) that would contain silent
  placeholders are refused. A share link prepares at most 20 missing tracks per request
  and refuses the archive while any silent placeholders remain; retrying prepares more.
- **"Restart Shijhon" stops what is playing.** A song may have to be started again in the
  app, and a track being added at that moment may have to be added again.

## Matching and what apps show

- **A catalog release can conflict with an existing album.** Shijhon checks whether it
  can safely add missing tracks to one existing album. Preparation stops when several
  albums fit, when the one that fits - or your album of the same name that is the
  release's album - is kept as it is, in the review list or already filled, when your
  album is complete by its files' track totals and the release is an edition of another
  name, or when it cannot take the release, when Navidrome would put the
  release's songs into another of your albums (the same album artist and album title,
  apart from letter case and the kind of quotation marks and dashes, and the same release
  date), when searching your library for the artist and the title finds over 20,000
  albums, or when neither can be searched (one letter each). Check the named album in
  Library; albums under **No match** have a **Match again** action.
- **Songs added to your album of the same name keep the release's numbers.** Where your
  album has another recording of a song (a live version), both are in the album, and two
  songs can have the same number.
- **An album whose title and artist are both written otherwise than the catalog's** (for
  example "NightLetters" by "TidePaper" for "Night Letters" by "Tide Paper") is not
  found: the catalog's release is added as a separate album beside it.
- **An automatic pick can be another edition than yours.** The Library page lists the
  picks. You can change one until the album is filled, not after.
- **A picked release keeps its track numbers.** Where your files are numbered
  differently, two songs of the album can have the same number.
- **An album in the review list, without a match, or kept as it is** has your songs only.
  The catalog's versions of it are separate albums in searches and on artist pages.
- **Artists who share a name.** An artist's page in your library shows the releases of the
  catalog artist with that name. If there are several, Shijhon looks at the first three
  and takes the one that shares the most album titles with your library, or none if none
  shares any. Top songs asked for by name can
  be another artist's.
- **An artist page can take two forms.** A catalog artist normally opens as your
  library's artist of that name. When the catalog does not answer in time, it opens as
  a catalog-only page.
- **Catalog entries show zeros.** Catalog albums in lists have a length of 0 until
  they are opened, and catalog artists an album count of 0.
- **Starring a catalog artist** works only once one of their albums is in your library.
- **A saved play queue** adds only its current song's album to the library. Another device
  that restores the queue does not see its other catalog songs.
- **Catalog track lists do not update** after a release is added to the library.

## The catalog of an add-on

An add-on's catalog ([catalogs.md](catalogs.md#the-catalog-of-an-add-on)) has less
to go on than an adapter's:

- **Albums partly in your library are matched less often.** The add-on sends no track
  or disc numbers, so an album whose files are numbered differently (a second disc, say)
  goes to the review list. A song without a length is not shown, and its album is
  never matched with one of yours.
- **Singles and EPs show as albums**, unless their title ends in "- Single" or "- EP".
  Clean and explicit versions are not marked.
- **A favorite or a playlist entry of a song from search results or top songs** looks
  its album up first, which takes a few seconds.
- **After a restart**, catalog songs an app still lists from an earlier search do not
  play until a search or an album shows them again.

## The cleanup

- **Short uses can be missed.** A playlist entry, queue, bookmark or share that begins and
  ends between two daily checks is not seen; favorites, ratings and plays always are.
- **It needs a running usage export.** Without a fresh export nothing is taken out
  ([deployment.md](deployment.md#the-usage-export)).
- **Old links can stop working.** When a release is added back, Navidrome can give a song
  another ID; apps' old IDs for it then lead nowhere.

## Apps that run in a browser

Music API answers, including catalog views, errors, covers and add-on audio, use
Navidrome's CORS policy. Browser apps on another origin can read them; preflight requests
are answered by Navidrome. Dashboard and sign-in origin checks remain separate.

## Audio other than music

An audiobook as an album of chapters, or a podcast as an artist with its episodes, is
served like music, with a catalog adapter and an add-on made for it. It has not been
tested yet. Audio whose length differs from the catalog's by more than 10 seconds or 5%
is taken for another recording (`length_tolerance_seconds`, `length_tolerance_percent`),
and a download or a conversion fetches the whole file first, up to 1 GiB and within three
minutes (`download_timeout_seconds`).

[Known gaps](reference/known-gaps.md) has the narrower cases.
