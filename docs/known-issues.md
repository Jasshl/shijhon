# Known issues

## Before you install

- **Placeholders are real files.** Every user of the library sees the ones that exist, and
  so do other programs that read your music folder
  ([hiding them](reference/advanced-deployment.md#hiding-placeholders-from-other-programs)).
  An app that talks to Navidrome directly plays their silence.
- **Navidrome's `Scanner.PurgeMissing` must stay `never`**, the default, or Shijhon
  writes nothing into the library ([why](deployment.md#what-shijhon-needs-from-navidrome)).
- **Any user can add releases.** Nothing limits how many unless an administrator sets a
  daily cap for each account ([the daily cap on adding](how-it-works.md#the-daily-cap-on-adding)).
  At the cap, a person still hears catalog songs that play as plain streams; those plays
  are not recorded, because recording one would add its release.
- **Catalog results appear in the API's ID3 methods** (`search3`, `getArtist`,
  `getAlbum`, `getAlbumList2`, `getStarred2`). Apps that browse by folders, and
  Navidrome's own API (its web interface, and apps set up as a Navidrome server rather
  than a Subsonic one), show your library without them.
- **Apps that sync the library and search on the device** show no catalog results.
- **One library, no `BaseURL`, and Navidrome 0.64.2**, the only version Shijhon is
  tested with.
- **A Linux host.** Docker Desktop on macOS and Windows is for a first look only, with
  the databases in Docker volumes, the default: in a folder of the Mac or Windows disk,
  Navidrome's database can be corrupted within minutes
  ([more](deployment.md#docker-desktop-on-macos-and-windows)).
- **Without JavaScript, the dashboard shows times in the server's time zone**, which is UTC
  when Shijhon runs in Docker. With JavaScript, the browser shows them in its own zone.

## Playback and downloads

- **HLS links do not play.** The next add-on is asked.
- **Navidrome's player transcoding and bitrate limits do not apply while catalog songs
  or placeholders stream from add-ons.** They play the add-on's audio unless the app
  asks for a `format` or `maxBitRate` in its stream request (`format=raw` keeps the
  original audio). Once real audio is in the library, Navidrome serves it normally.
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
  picks, and a pick can be changed there. A filled album can be filled from another of
  the releases that fit it instead (**Wrong edition?**).
- **A filled album keeps its release once anyone has used a song the fill added.** A
  play, a favorite, a rating, a playlist entry, a play queue, a bookmark, a share, a
  stream or download Shijhon served, or downloaded audio in a song's place counts, also
  when it has ended, as for the cleanup. The release is then changed for none of its
  songs. The choice also needs Navidrome's usage records (the
  [usage export](deployment.md#the-usage-export)), as the cleanup does. The releases
  offered are those the album's match considered.
- **After an album is filled from another release, an app can still hold the old songs.**
  Starring, rating, queueing or reporting a play of one that the new release does not
  have under the same number and title answers "not found" until the app has loaded the
  album again, and "try again later" while the replacement is not finished. A song both
  releases have under the same number and title keeps its ID, also when the recordings
  differ ([how it works](how-it-works.md#matching-and-filling)).
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
- **Track lists follow the catalog slowly, and only in part.** The library pass reads
  the track list of each release in your library again about once a month, and once a
  week while the release is new ([how it works](how-it-works.md#track-lists-read-again)).
  It adds tracks, and corrects the length of songs that have no audio yet. A title, an
  artist, an ISRC or a track number the catalog changed stays as it was, and no song is
  removed, so a track the catalog puts where another song already is, is not added, and
  a track the catalog no longer lists stays. With the library pass off, or as a dry run,
  nothing is updated.

- **New releases show in the newest albums of apps that ask the server for them.** They
  are added to the ID3 list (`getAlbumList2` with `type=newest`). An app that browses by
  folders, or that builds its own list of recent albums from a synced copy of the
  library, shows them only if it takes them from that list. An app that syncs the whole
  library through that list keeps the new releases as albums until its next sync after
  they left.
- **New releases follow the catalog's dates, once a day.** A release shows from the next
  daily check after the catalog lists it, and one the catalog dates by its year alone
  never shows as new. For several catalog artists of one name the check picks the one
  whose releases share the most titles with your library, and none when no title is
  shared.

## The catalog of an add-on

An add-on's catalog without the optional
[full-catalog extension](reference/add-on-protocol.md#the-full-catalog-extension)
has less to go on than an adapter's:

- **Albums partly in your library are matched less often.** The add-on sends no track
  or disc numbers, so an album whose files are numbered differently (a second disc, say)
  goes to the review list. A song without a length is not shown, and its album is
  never matched with one of yours.
- **Singles and EPs show as albums**, unless their title ends in "- Single" or "- EP".
  Clean and explicit versions are not marked.
- **A favorite or a playlist entry of a song from search results or top songs** looks
  its album up first, which takes a few seconds.
- **A radio from a song, an album or an artist that is only in the catalog needs
  ListenBrainz or Last.fm.** With ListenBrainz turned off, it has songs only for a person
  who set up Last.fm on the Playlists page
  ([how it works](how-it-works.md#radio-from-catalog-items)). The first radio from an item
  can be short when the catalog answers slowly: it starts with the songs found within the
  wait, and asked for again it has them all.
- **A radio holds what the sources list under the same names.** Each similar song is
  looked up in the catalog by its artist and title. One listed under another spelling, or
  that the catalog does not have, is left out. ListenBrainz has songs only for a song's
  canonical MusicBrainz recording; one that neither its exact lookup nor its recording
  search (nor, with a token, its metadata lookup) finds has no ListenBrainz songs.
- **ListenBrainz's songs join a radio the next time.** For a person with Last.fm, the
  first radio from an item has Last.fm's songs alone; ListenBrainz's are added in the
  background.
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
