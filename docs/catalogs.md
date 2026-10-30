# Catalogs

The catalog provides search results, artist discographies, album track lists, an
artist's top songs and covers; the audio comes from add-ons. It comes from a *catalog
adapter*, a separate package installed into Shijhon's image, or from one of your add-ons
that has a catalog. The default is none.

## Installing an adapter

With the Compose file, set `SHIJHON_ADAPTERS` in `packaging/.env` to the adapter's
address: its source archive or its wheel. Then build the image again from `packaging/`
with `docker compose up -d --build`; `docker compose exec shijhon shijhon catalogs` then
lists the adapter. The [quick start](quick-start.md#what-to-add-next) has the line for the
MusicBrainz adapter. An adapter that is not published is installed from its wheel
([deployment.md](deployment.md#catalog-adapters)); to write one, see
[development.md](development.md#writing-a-catalog-adapter).

## Choosing an adapter

On the dashboard's Catalog page: choose an installed adapter and save, enter the
settings the page then shows and save again, and press **Restart Shijhon**. Or in the
configuration file:

```toml
[catalog]
kind = "example"          # the adapter's name
# ... and the adapter's own settings (its region, its credentials), as its notes say
```

- An adapter's settings belong to its `kind`. Choosing another catalog on the dashboard
  removes what was saved for the one before.
- Shijhon does not start with a `kind` whose adapter is not installed; its error lists the
  installed ones.
- Removing an adapter leaves its placeholders in the library. They keep playing; new
  searches, artist pages and fills need a catalog again.

## The catalog of an add-on

The separate [Shijhon Mornye extension bridge](https://github.com/Jasshl/shijhon-mornye-bridge)
makes Mornye extensions usable as add-ons, including catalogs when an extension supplies
them. It is not affiliated with Mornye.

An add-on that has a catalog of its own (a search, albums, artists) can be the
catalog, with nothing to install. On the Catalog page choose **An add-on's catalog**
and save, choose the add-on and save again, and press **Restart Shijhon**. Or:

```toml
[catalog]
kind = "addon"
addon = "My add-on"       # its name, as on the Add-ons page or in [[addons]]
```

- Catalog requests count against that add-on's [limits](add-ons.md#limits): a search, an
  album and an artist page take one request each, plus any release pages.
- Without a confirmed identity, Shijhon keeps what it learned under the add-on's name.
  Renaming the add-on, or choosing another one, starts over: what is in your library stays and plays,
  and albums not filled yet are matched again. If you set an add-on up so that its
  catalog changes, rename it too.
- An add-on with the optional [full-catalog extension](reference/add-on-protocol.md#the-full-catalog-extension)
  can supply disc and track numbers, release kinds, version flags, song and ISRC
  lookups, and paged discographies. Add-ons without it have the limits in
  [known-issues.md](known-issues.md#the-catalog-of-an-add-on).

A full-catalog add-on can declare a stable catalog identity: a key and optional region.
The Catalog page reads the chosen add-on's manifest and shows “This add-on says its
catalog is …”. Check the claim, select **Confirm this identity** and save, then restart
Shijhon. When you choose another add-on, its claim is shown before it is saved. In a
configuration file, confirm it yourself:

```toml
[catalog]
kind = "addon"
addon = "My add-on"
identity = "examplemusic.us"  # the declared key.region; key alone without a region
```

Confirm an identity only when the add-on uses that catalog's own item IDs with the same
meaning. This lets you move from an adapter to an add-on, or between add-ons, while
keeping the catalog key and region: saved matches and catalog lists continue under
the same scope. While an adapter is still the configured catalog, an add-on declaring
the same key gets no direct catalog IDs: it is looked up like any add-on.
No image rebuild is needed to choose an add-on. Before catalog requests, including
cached answers, a confirmed catalog checks its manifest again when the last check is
at least five minutes old. An answer naming a different item also triggers a check.
Checks share one request, run at background urgency and wait at most two seconds
(or the catalog timeout, if shorter). A failed check keeps the last known declaration
and waits five minutes before another age-based attempt. Once a changed or missing
claim is read, catalog requests fail safely and views answer from the library until
you check and confirm the identity on the Catalog page. Nothing is re-keyed silently;
changes between checks cannot be detected immediately. Library music stays in place.

Leaving the identity empty keeps today's name-based items. The page offers to confirm
any declared identity. Confirming later changes the catalog key, so matches and saved
catalog lists start over. Library music stays and plays. Artwork addresses reach apps
only if the add-on opts in, and only for public HTTPS hosts; other artwork uses cover IDs.

Playlist imports also search the catalogs of other enabled add-ons that have one, for
entries that neither your library nor the configured catalog has
([playlist-imports.md](playlist-imports.md#add-on-catalogs)). This does not change the
configured catalog and needs no restart.

## A catalog that answers slowly

A search, the page of an artist in your library and an artist's top songs wait up to 8
seconds for the catalog ("Wait for the catalog" on the Catalog page, `[search]
budget_seconds`). When it takes longer, the answer has only your library's results, and
the same search a moment later has the catalog's too. Raise the wait for a catalog
that needs longer; an adapter's notes should say by how much. Pages of artists and albums
not in your library wait until the catalog answers or fails. After a search or an
artist page found the catalog unavailable or busy, searches are answered only from the
library for half a minute.

## The MusicBrainz adapter

An example adapter: [`shijhon-catalog-musicbrainz`](https://github.com/Jasshl/shijhon-catalog-musicbrainz),
for [MusicBrainz](https://musicbrainz.org), the open music encyclopedia, with its own
repository and notes. It needs no account or key
(`kind = "musicbrainz"`).

- **First searches and pages take a few seconds**: MusicBrainz allows one request a
  second, so a first search takes four to five seconds. Once Shijhon has cached a page, it
  appears at once.
- **No artist pictures**, and some albums have no cover.
- **An artist's top songs need a ListenBrainz token** (a setting of the adapter).
