# Quick start with Docker Compose

```sh
git clone https://github.com/Jasshl/shijhon && cd shijhon
./setup.sh
```

The script runs on Linux and macOS; on Windows, run it inside WSL. It has not been
tested in WSL yet. Already running Navidrome? To keep its users, IDs,
playlists and favorites, see
[deployment.md](deployment.md#starting-from-an-existing-navidrome).

It asks where your music is, for a username and a password for your own account, for the
port your apps connect to (4533 by default), and which catalog adapters to install (none
by default). Then it checks Docker, the music folder and the port, and creates the
placeholder folder `_shijhon` in your music folder. It writes `packaging/.env` and
`packaging/config/shijhon.toml`, and creates your account and Shijhon's own admin account,
`shijhon`, whose password goes into `packaging/config/navidrome-password`. Last, it builds
the image and starts everything. If you run it again, it continues from what is already
there; `./setup.sh --help` lists its options.

The [Compose file](../packaging/compose.yaml) runs Navidrome, which only Shijhon can
reach; Shijhon itself; and the usage export, a small helper with no network access that
tells Shijhon which releases are used. Their data is in Docker volumes
([where the data is](deployment.md#where-the-data-is)).

## Connecting

Connect your apps to Shijhon, never to Navidrome: `http://127.0.0.1:4533` on this
machine, `http://<this machine's address>:4533` from other devices. Add it as a
**Subsonic** (or OpenSubsonic) server, even in apps that offer a Navidrome option: that
option uses Navidrome's own API for some or all of what the app shows, and that API
shows your library without the catalog ([known issues](known-issues.md#before-you-install)).
Navidrome's web interface is at `/app` (to add other people's accounts), the dashboard
at `/shijhon/`. To check the installation, play something from your library and look at
the dashboard's Diagnostics page.

Before first using a music app, sign in to **Collection** in the dashboard to prepare your
library. Apps default to **All**: everything you collected - whole albums of your imports,
your imported playlists and your favorites (**Original imported library**), and of what
Shijhon added from the catalog because you played it, favorited it, put it into a
playlist, downloaded it or got it in a playlist Shijhon made for you (**Shijhon library**)
- plus music you add on that page ([Your collection](collection.md)).

To upload your own music files, enable the optional
[music-import storage](deployment.md) and open **Collection → Import music**.
An administrator first reviews who imported the existing server music. After
that setup, new accounts start empty; each person's uploads join their
**Original imported library** and **All**. Other people can choose shared music without
receiving import credit.

The connection is not encrypted, as with a plain Navidrome, so this is for a network you
trust. For access from outside, put a TLS reverse proxy in front
([deployment.md](deployment.md#network-and-reverse-proxy)).

## What to add next

Add add-ons, which supply the audio for catalog tracks, on the dashboard's **Add-ons**
page ([add-ons.md](add-ons.md)).

Then choose a catalog on the dashboard's **Catalog** page: an installed catalog adapter,
or the catalog of one of your add-ons ([catalogs.md](catalogs.md)). An adapter is a
separate package built into Shijhon's image: give its address when the script asks for
catalog adapters, or add it to `.env` later and build again
([installing an adapter](catalogs.md#installing-an-adapter)).

## By hand

The same installation, from `packaging/`.

1. **Write `.env`**, and create `_shijhon` before the first start (otherwise Docker
   creates it owned by root). The containers run as `PUID:PGID` (1000:1000 unless set),
   which must be able to read the music; it is mounted read-only, except `_shijhon`.

   ```sh
   cat > .env <<EOF
   MUSIC_DIR=/path/to/music
   PUID=$(id -u)
   PGID=$(id -g)
   EOF
   . ./.env
   mkdir -p "$MUSIC_DIR/_shijhon"
   ```

   Optional: `SHIJHON_PORT=4533`; `SHIJHON_BIND=127.0.0.1` (this machine only);
   `NAVIDROME_DATA`, `SHIJHON_DATA`, `USAGE_EXPORT_DATA` (a folder instead of a Docker
   volume; not with Docker Desktop).

2. **Copy the configuration** and keep it private: it will hold a password, add-on URLs
   and catalog access. The example needs no change.

   ```sh
   mkdir -p config
   cp shijhon.example.toml config/shijhon.toml
   chmod 600 config/shijhon.toml
   ```

3. **Start Navidrome, then create the accounts**: Shijhon's service account, an admin
   account used only by Shijhon, then your own. Each asks for its password. Put the
   service account's in `config/navidrome-password`, private like the configuration (both
   readable by `PUID`).

   ```sh
   docker compose up -d navidrome
   until docker compose logs navidrome | grep -q "server is ready"; do sleep 1; done
   docker compose run --rm -it navidrome user create -u shijhon --admin
   docker compose run --rm -it navidrome user create -u yourname --admin
   (umask 077; read -rs pw && printf '%s\n' "$pw" > config/navidrome-password && unset pw)
   chmod 600 config/navidrome-password
   ```

4. **Start everything** (the first start builds the image), then
   [connect your apps](#connecting):

   ```sh
   docker compose up -d --build
   docker compose logs -f shijhon
   ```
