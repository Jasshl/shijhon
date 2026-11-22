# Known issues

These are the limits you are likely to run into. Narrower cases are in the
[known gaps](reference/known-gaps.md).

## Before you install

- **Catalog songs in the library are silent files until their audio is saved.** These
  [placeholder](how-it-works.md#placeholders) files are real: every user of the library
  sees them, and so do other programs that read the music folder ([hiding
  them](reference/advanced-deployment.md#hiding-placeholders-from-other-programs)). An app
  that connects to Navidrome directly, not through Shijhon, plays them as silence.
- **Navidrome's `Scanner.PurgeMissing` must stay `never`**, the default, or Shijhon
  writes nothing into the library ([why](deployment.md#what-shijhon-needs-from-navidrome)).
- **Any user can add releases.** Nothing limits how many unless an administrator sets a
  daily cap for each account ([the daily cap on adding](how-it-works.md#the-daily-cap-on-adding)).
- **Catalog results show only where apps browse by artist and album** (the API's ID3
  methods). Apps that browse by folders, and Navidrome's own API (its web interface, and
  apps set up as a Navidrome server rather than a Subsonic one), show your library
  without catalog results.
- **Apps that sync the library and search on the device** show no catalog results.
- **Shijhon supports one library, no `BaseURL`, and Navidrome 0.64.2**, the only version
  it is tested with.
- **It needs a Linux host.** Docker Desktop on macOS and Windows is for a first look only.
  Keep the databases in Docker volumes, the default. In a folder on the Mac or Windows
  disk, Navidrome's database can be corrupted within minutes
  ([more](deployment.md#docker-desktop-on-macos-and-windows)).
- **Without JavaScript, the dashboard shows times in the server's time zone**, which is UTC
  when Shijhon runs in Docker. With JavaScript, the browser shows them in its own zone.

## Playback and downloads

- **HLS links do not play.** When an add-on answers with one, the next add-on is asked.
- **Navidrome's transcoding settings and bitrate limits don't apply to songs streamed from
  add-ons.** Those songs play the add-on's audio as it is, unless the app itself asks for
  another format or bitrate. Once a song's audio is saved in the library, Navidrome's
  settings apply again.
- **A song below your minimum audio quality can still play from its own file**, for
  example when the app asks for a conversion or sets a bitrate limit. When it plays from
  the add-ons instead, it takes as long to start as a catalog song
  ([details](reference/known-gaps.md#playback-and-add-ons)).
- **An imported song is upgraded only after a plain play by its importer**, not after a
  conversion, a download, a share link or someone else's play
  ([details](reference/known-gaps.md#music-imports)).
- **A stream that has to be converted starts late.** When Navidrome would convert the
  audio - another format, or a lower bitrate than the add-on's audio has - Shijhon fetches
  the whole file first. If the fetch fails, the song plays in the add-on's own format.
- **Apps are told a streamed song is FLAC.** Until its audio is saved in the library,
  Navidrome only knows the song's silent placeholder, which is a FLAC file. The audio an
  app actually gets is in whatever format the add-on sends.
- **Archive downloads** (a whole album, artist or playlist) that would contain silent
  placeholders are refused. A share link prepares its missing audio in batches, so
  opening it again prepares more
  ([details](reference/known-gaps.md#playback-and-add-ons)).
- **"Restart Shijhon" stops what is playing.** A song may have to be started again in the
  app, and a track being added at that moment may have to be added again.

## Matching and what apps show

- **A catalog release can conflict with an existing album.** When Shijhon cannot safely
  add the missing tracks to one album of yours, it stops and names the album. Check it in
  Library; albums under **No match** have a **Match again** action. The
  [cases](reference/known-gaps.md#library-writes-and-the-cleanup) are listed in the known
  gaps.
- **Songs added to your album keep the release's track numbers.** Where your files are
  numbered differently, or your album has another recording of a song (a live version),
  two songs can have the same number.
- **An album whose title and artist are both written differently from the catalog's** (for
  example "NightLetters" by "TidePaper" for "Night Letters" by "Tide Paper") is not
  found: the catalog's release is added as a separate album beside it.
- **An automatic pick can be another edition than yours.** The Library page lists the
  picks, and a pick can be changed there. A filled album can be filled from another of
  the releases that fit it instead (**Wrong edition?**).
- **A filled album keeps its release once anyone has used a song the fill added**, for
  example with a play, a favorite or a playlist entry
  ([details](reference/known-gaps.md#library-writes-and-the-cleanup)).
- **After an album is filled from another release, an app can still hold the old songs.**
  Starring or playing one of them can answer "not found" until the app has loaded the
  album again ([details](reference/known-gaps.md#library-writes-and-the-cleanup)).
- **An album in the review list, without a match, or kept as it is** has your songs only.
  The catalog's versions of it are separate albums in searches and on artist pages.
- **Artists who share a name.** An artist's page in your library shows the releases of the
  catalog artist with that name. When several catalog artists have that name, it shows
  the one that shares the most album titles with your library. Top songs asked for by name
  can belong to another artist.
- **An artist page can take two forms.** A catalog artist normally opens as your
  library's artist of that name. When the catalog does not answer in time, it opens as
  a catalog-only page.
- **Catalog entries show zeros.** Catalog albums in lists have a length of 0 until
  they are opened, and catalog artists an album count of 0.
- **Starring a catalog artist** works only once one of their albums is in your library.
- **A saved play queue** adds only its current song's album to the library. Another device
  that restores the queue does not see its other catalog songs.
- **Track lists follow the catalog slowly, and only in part.** About once a month, and
  once a week while a release is new, the library pass adds tracks the catalog added and
  corrects the length of songs that have no audio yet. Other catalog changes are not
  followed, and no song is removed
  ([how it works](how-it-works.md#track-lists-read-again)).
- **New releases show in the newest albums of apps that ask the server for them.** An app
  that browses by folders, or that builds its own list of recent albums from a synced
  copy of the library, shows them only if it reads the server's list of newest albums
  ([details](reference/known-gaps.md#catalog-and-views)).
- **New releases follow the catalog's dates, once a day.** A release shows from the next
  daily check after the catalog lists it, and one the catalog dates by its year alone
  never shows as new.

## The catalog of an add-on

An add-on's catalog without the optional
[full-catalog extension](reference/add-on-protocol.md#the-full-catalog-extension)
has less to go on than a complete catalog:

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
  can be short when the catalog answers slowly. Asked for again, it has all its songs.
- **A radio holds only the similar songs the catalog has under the same names.** A
  similar song listed under another spelling, or one the catalog does not have, is left out
  ([in detail](reference/how-it-works.md#radio-from-catalog-items)).
- **After a restart**, catalog songs an app still lists from an earlier search do not
  play until a search or an album shows them again.

## The cleanup

- **Short uses can be missed.** A playlist entry, queue, bookmark or share that begins and
  ends between two daily checks is not seen; favorites, ratings and plays always are.
- **It needs a running usage export.** Without a fresh export nothing is taken out
  ([deployment.md](deployment.md#the-usage-export)).
- **Old links can stop working.** When a release is added back, Navidrome can give a song
  another ID, and apps' old IDs for it then lead nowhere.

## Audio other than music

With an add-on (or a catalog adapter) made for it, an audiobook is served like music as an
album of chapters, and a podcast as an artist with its episodes. This has not been tested
yet. Audio whose length differs from the catalog's by more than 10 seconds or 5% is taken
for another recording. A download or a conversion fetches the whole file first, up to
1 GiB and within three minutes
([the settings](reference/settings.md#delivery)).
