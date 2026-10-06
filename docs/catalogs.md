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

An add-on that has a catalog of its own (a search, albums, artists) can be the
catalog, with nothing to install. On the Catalog page choose **An add-on's catalog**
and save, choose the add-on and save again, and press **Restart Shijhon**. Or:

```toml
[catalog]
kind = "addon"
addon = "My add-on"       # its name, as on the Add-ons page or in [[addons]]
```

- Catalog requests count against that add-on's [limits](add-ons.md#limits): a search, an
  album and an artist page take one request each.
- Shijhon keeps what it learned from the catalog under the add-on's name. Renaming the
  add-on, or choosing another one, starts over: what is in your library stays and plays,
  and albums not filled yet are matched again. If you set an add-on up so that its
  catalog changes, rename it too.
- It has less to go on than an adapter's catalog:
  [known-issues.md](known-issues.md#the-catalog-of-an-add-on).

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
