# How it works

Your apps talk to Shijhon as they would to Navidrome, and Shijhon passes their requests on
unchanged. To the answers for searches, artist pages and albums it adds results from the
catalog. Navidrome checks every login.

## Placeholders

- **When you use a catalog track** - the app's "now playing" report, a favorite, a
  rating, a playlist entry, a bookmark, a share, a download, or a stream that has to be
  converted - Shijhon writes its whole release into a **placeholder folder** in your
  library: silent FLAC files with the right tags. Navidrome scans them, so they become
  normal tracks with real IDs. Looking at catalog items, and playing them as plain
  streams, writes nothing.
- **Streams** come from your add-ons and are never stored in your library.
- **Downloads and conversions** (to another format, or a lower bitrate than the add-on's
  audio has), Navidrome's jukebox and its share links: Shijhon fetches the whole song
  first and puts it in place of the placeholder, and Navidrome serves it from there. After
  30 days unused, the song is replaced by its silent placeholder again; once such audio
  takes more than 10 GB in total, the least recently used are replaced first
  ([`delivered_days`, `delivered_gb`](configuration.md#delivery)).
- **A track found in your library** on another release (the single of an album track, say)
  plays your own file.

## Matching and filling

- Albums partly in your library are matched against the catalog when they are first
  shown, and by a background pass over the whole library. Uncertain matches go to the
  review list on the dashboard's Library page.
- A matched album is shown complete, with the complete album's song count and length.
  Nothing else Navidrome says about your own music is changed, and your files are never
  modified.
- Its missing tracks are written as placeholders into the same album when you first use
  the album or one of them. They are also added automatically for albums you have enough
  of (3 songs, or a quarter of the album), depending on `[fill] library_pass`:
  - `"dry_run"`, the default: the pass records what it would fill, and fills nothing;
  - `"on"`: the pass fills those albums, and so does opening one in an app;
  - `"off"`: no pass; such an album is filled when it is shown.

## The cleanup

Off by default. After a set number of days, it removes catalog releases and fills that
nobody has used. It takes out whole releases only. A release stays once a daily check has
seen any user use it: a favorite, a rating, a play, a playlist entry, a queue, a bookmark
or a share, even one removed since. The daily checks note these uses whether the cleanup
is on or not, so set the [usage export](deployment.md#the-usage-export) up early. A
release taken out comes back, normally with the same song IDs, when an app uses one of its
old IDs in a way that adds songs to the library. A removed fill leaves its album shown as
complete.
