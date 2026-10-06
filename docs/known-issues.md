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
- **A stream that has to be converted starts late.** When Navidrome would convert the
  audio - another format, or a lower bitrate than the add-on's audio has - Shijhon fetches
  the whole file first. If the fetch fails, the song plays in the add-on's own format.
- **Apps are told the song is FLAC.** Until a song's audio is in the library, Navidrome
  describes it as its placeholder, a FLAC file. The audio an app gets is the add-on's.
- **Archive downloads** (a whole album, artist or playlist) that would contain silent
  placeholders are refused.
- **"Restart Shijhon" stops what is playing.** A song may have to be started again in the
  app, and a track being added at that moment may have to be added again.

## Matching and what apps show

- **Before a partly owned album is matched**, using a catalog song of its release adds
  the release as a separate album. Your album then goes to the review list.
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

Shijhon's own answers (catalog views, its errors, catalog covers, audio from add-ons)
carry no CORS headers, so a browser app on another origin cannot read them.

## Audio other than music

An audiobook as an album of chapters, or a podcast as an artist with its episodes, is
served like music, with a catalog adapter and an add-on made for it. It has not been
tested yet. Audio whose length differs from the catalog's by more than 10 seconds or 5%
is taken for another recording (`length_tolerance_seconds`, `length_tolerance_percent`),
and a download or a conversion fetches the whole file first, up to 1 GiB and within three
minutes (`download_timeout_seconds`).

[Known gaps](reference/known-gaps.md) has the narrower cases.
