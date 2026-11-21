# Deployment

Shijhon runs in front of a stock Navidrome that only Shijhon reaches.
[quick-start.md](quick-start.md) sets up a new one with the Compose file and the example
configuration in [`packaging/`](../packaging), and
[below](#in-front-of-a-navidrome-you-already-run) Shijhon goes in front of a Navidrome you
already run. [Advanced deployment](reference/advanced-deployment.md) has the rarer setups.

On NixOS, [the NixOS service](nixos.md) sets up Navidrome and Shijhon together, also with
a Navidrome you already run. [Its reference](reference/nixos.md) covers catalog adapters
and moving from the Docker setup.

## In front of a Navidrome you already run

Shijhon can go in front of the Navidrome you run now, which keeps running as it is, with
its accounts, IDs, playlists, favorites and play counts. Nothing is copied: Shijhon reaches
your Navidrome over the network, Compose starts no Navidrome of its own, and you change two
settings of yours.

This needs Docker Compose 2.24.4 or newer (`docker compose version`). Run `./setup.sh` in
the checkout and answer `running` to its first question. It then asks
for the music folder your Navidrome scans, your Navidrome's address, an admin account of
yours, your Navidrome's data for the usage export, and the port your apps connect to. The
admin account is used once, to create Shijhon's own admin account, `shijhon`. Its password
is not saved. The script makes Shijhon's Docker network, `<project>-navidrome`: that is
`shijhon-navidrome`, or `<name>-navidrome` with another `COMPOSE_PROJECT_NAME`. Its range is
one that nothing on the machine uses (`SHIJHON_NETWORK` in `packaging/.env`), and Shijhon's
address there is always `.10`, for example `172.30.99.10`. Once the image is built, the
script prints the changes your Navidrome needs, with the network's name and Shijhon's
address, and at a terminal waits until you press Enter once your Navidrome has joined.
Without a terminal it stops there, and the next run goes on. If Shijhon still can't reach
your Navidrome, the script says so and offers to try again.

The script writes `COMPOSE_FILE=compose.yaml:compose.existing-navidrome.yaml` into
`packaging/.env`. The [second file](../packaging/compose.existing-navidrome.yaml) leaves out
the Compose file's Navidrome and uses that network. Later Compose commands in `packaging/`
use both files.

### Your music folder

Give the folder your Navidrome scans, as this machine has it: for a Navidrome in a
container, the folder on the host that is mounted as its music folder, not a folder above
or inside it. Shijhon creates its placeholder folder, `_shijhon`, in that folder, so
Navidrome finds the files Shijhon writes inside its own library, at the path it has for
it. Navidrome can keep the folder mounted read-only. Shijhon's container runs as `PUID:PGID`
from `packaging/.env`, your user, which has to be able to read the music and write
`_shijhon`.

If your Navidrome has more than one library, Shijhon uses the first one. For another, set
`library_id` under `[navidrome]` in `packaging/config/shijhon.toml` to its ID
(`getMusicFolders` lists them) and restart Shijhon.

### Your Navidrome trusts Shijhon

Navidrome limits failed logins per client address. It learns each app's address from
Shijhon only when it trusts the address it sees Shijhon at, its `ExtAuth.TrustedSources`
(`ND_EXTAUTH_TRUSTEDSOURCES` in its environment). Without that, all apps count as one, and
a device with an old password locks that person out on every other device for a while
([client addresses](reference/advanced-deployment.md#client-addresses)). Trust only
Shijhon's address, written as a network (`/32`) without spaces, and make sure apps can no
longer reach Navidrome directly. Which address that is depends on where your Navidrome
runs.

**A Navidrome in a container on this machine**, in a Compose project of its own or started
with `docker run`, joins Shijhon's network. There it sees Shijhon at Shijhon's own address,
and Shijhon reaches it by its service or container name, for example
`http://navidrome:4533`. In your Navidrome's Compose file, with the range and the network
name the script printed in place of `172.30.99` and `shijhon-navidrome`:

```yaml
services:
  navidrome:
    environment:
      ND_EXTAUTH_TRUSTEDSOURCES: 172.30.99.10/32
    networks: [default, shijhon]
    # Remove its "ports:" once your apps use Shijhon.
networks:
  shijhon:
    name: shijhon-navidrome
    external: true
```

Then run `docker compose up -d` in your Navidrome's folder. A container started with
`docker run` joins with `docker network connect <project>-navidrome <its name>`, which lasts
until the container is created again, and gets the setting with
`-e ND_EXTAUTH_TRUSTEDSOURCES=172.30.99.10/32` when you create it.

**A Navidrome on this machine outside Docker** is reached at
`http://host.docker.internal:4533` (with its port). On Linux, which has not been tried yet,
that name is the address of Docker's bridge, usually `172.17.0.1`. Navidrome then listens
on every address (`ND_ADDRESS=0.0.0.0`), with a firewall that keeps its port from the
network but lets Shijhon's range in, for example with ufw
`sudo ufw allow from 172.30.99.0/24 to any port 4533 proto tcp`. It sees Shijhon at
Shijhon's own address, `172.30.99.10/32` in the example. With Docker Desktop on a Mac,
Navidrome can keep listening on `127.0.0.1`, and it sees Shijhon at `127.0.0.1`.
Trusting `127.0.0.1/32` trusts every program on that computer, which can then sign in to
Navidrome as anyone, so only do that where you trust them all.

**A Navidrome on another machine** is reached at its address, for example
`http://192.168.1.20:4533`. It sees Shijhon at this machine's address on your network, which
is the one to trust; let only this machine reach Navidrome's port, with a firewall on
Navidrome's machine. Shijhon also needs the same music files: share Navidrome's music folder
with this machine (over NFS or SMB, for example), give the shared folder as the music folder,
and let your user write `_shijhon` in it. Leave out the usage export, because a database
shouldn't be read over a network share.

If your Navidrome signs people in through a reverse proxy's header (`ExtAuth.UserHeader`
or `ReverseProxyUserHeader`), see
[sign-in by the reverse proxy](reference/advanced-deployment.md#sign-in-by-the-reverse-proxy).
Your Navidrome must also meet [what Shijhon needs](#what-shijhon-needs-from-navidrome).

### Moving your apps

Your apps then connect to Shijhon, at the port you gave, with the same accounts. Add
Shijhon as a **Subsonic** server ([connecting](quick-start.md#connecting)). Remove your
Navidrome's published port, so that nothing reaches it except through Shijhon: apps that
still connect to it directly would see Shijhon's placeholders there as silent songs. To give
Shijhon the port your apps used, set `SHIJHON_PORT` in `packaging/.env` to it once
Navidrome no longer uses it, and run `docker compose up -d` in `packaging/`. A reverse
proxy that sent people to Navidrome now sends them to Shijhon instead
([network and reverse proxy](#network-and-reverse-proxy)).

### Taking Shijhon out again

Stop Shijhon with `docker compose down` in `packaging/`. Your Navidrome still lists the
songs Shijhon wrote into `_shijhon`, as silent files: delete that folder, let Navidrome
scan, and remove the missing files in its web interface. Then take the network and
`ND_EXTAUTH_TRUSTEDSOURCES` out of your Navidrome's settings and restart it, remove the
network with `docker network rm <project>-navidrome`, and delete the `shijhon` account under
**Users**.

### The usage export with your Navidrome

The cleanup needs [the usage export](#the-usage-export), which reads your Navidrome's
database read-only. Give the script your Navidrome's data folder, the one mounted as its
`/data` (it holds `navidrome.db`), or the name of its Docker volume (`docker volume ls`
lists them). The export runs as `PUID:PGID` and needs to be able to read those files: the
files of a Navidrome container that runs as root can be read by everyone, and so can those
of one that runs as your user.

Without it, Shijhon can't tell whether anyone used a catalog release it added: the cleanup
doesn't run, and when an album is filled from another release, the earlier release's songs
stay in the library. Everything else works. To add it later, add
`EXISTING_NAVIDROME_DATA='/path'` (a folder) or `EXISTING_NAVIDROME_VOLUME=name` (a volume)
and `COMPOSE_PROFILES=usage-export` to
`packaging/.env`, uncomment `usage_export_path` under `[navidrome]` in
`packaging/config/shijhon.toml`, and run `docker compose up -d` in `packaging/`.

### By hand

The same, from `packaging/`:

1. Write `.env` and the configuration as in the quick start's
   [steps by hand](quick-start.md#by-hand), steps 1 and 2, with `MUSIC_DIR` your Navidrome's
   music folder, and add to `.env`:

   ```dotenv
   SHIJHON_NETWORK=172.30.99
   COMPOSE_FILE=compose.yaml:compose.existing-navidrome.yaml
   ```

   In `config/shijhon.toml`, set `url` under `[navidrome]` to your Navidrome's address, and
   for the usage export follow [the section above](#the-usage-export-with-your-navidrome),
   or comment out `usage_export_path`.

2. Make Shijhon's network with that range, and connect your Navidrome to it
   ([above](#your-navidrome-trusts-shijhon)). It is named `shijhon-navidrome`, or
   `<name>-navidrome` with `COMPOSE_PROJECT_NAME=<name>` in `.env`. Other containers on it
   get addresses from `.128` up, so `.10` stays Shijhon's:

   ```sh
   docker network create --subnet 172.30.99.0/24 --ip-range 172.30.99.128/25 shijhon-navidrome
   ```

3. Create Shijhon's account in your Navidrome's web interface, under **Users**, as an
   admin, with a long random password. Put that password in `config/navidrome-password`,
   readable only by you, as in the quick start's step 3.

4. Start Shijhon:

   ```sh
   docker compose up -d --build
   ```

### What Shijhon needs from Navidrome

- **Version 0.64.2**, unmodified, **not reachable from outside**, at the root of its
  address (no `BaseURL`).
- **A service account**: a Navidrome admin account used only by Shijhon, not a person's.
- **The same library files** in Shijhon's container (at `/music` there, whatever path
  Navidrome has them at), read-only, with only the placeholder folder writable - unless you turn on **Also fix files in your music folder**
  ([how it works](how-it-works.md#each-artist-of-a-joined-credit)): then mount the library
  writable for Shijhon (in `compose.yaml`, drop `:ro` from the `/music` line); only files owned by
  Shijhon's user are changed.
- **`Scanner.PurgeMissing = never`**, its default. With another value a placeholder that
  is gone for a moment would lose every user's favorites and playlist entries of it, so
  Shijhon then writes nothing into the library: such actions are answered with the reason,
  and downloads get the add-on's own format. Shijhon reads the value from Navidrome's
  configuration, which Navidrome shows its admins while `DevUIShowConfig` is on (its
  default). With that off, set `[navidrome] purge_missing = "never"` yourself.

## Music imports

Optional [personal music-file imports](music-imports.md) need an additional
dedicated writable directory. In Compose, provision `MUSIC_IMPORTS_DIR`, give
`PUID:PGID` access and add it to `packaging/.env`. Keep the
[`compose.imports.yaml`](../packaging/compose.imports.yaml) overlay enabled by adding:

```dotenv
COMPOSE_FILE=compose.yaml:compose.imports.yaml
```

Then start from `packaging/`:

```sh
docker compose up -d --build
```

With Shijhon [in front of a Navidrome you already run](#in-front-of-a-navidrome-you-already-run),
keep its second file in the list,
`COMPOSE_FILE=compose.yaml:compose.existing-navidrome.yaml:compose.imports.yaml`, and mount
the same directory into your Navidrome as `_imports` inside its music folder, read-only.

Compose and `./setup.sh` use this file set on later runs. If an existing project was
started with `-f` instead, setup reads its containers' Compose file set and saves it in
`packaging/.env` when no `COMPOSE_FILE` is specified. An explicit `COMPOSE_FILE` in the
shell or `.env` takes precedence; keep the intended set in `.env` for future commands.
The overlay refuses a missing host directory, exposes published originals read-only to
Navidrome and enables imports in Shijhon. Include this entire directory and Shijhon's
state in backups; it must
not be treated as disposable catalog audio. Complete the reviewed administrator
setup from **Collection → Import music** before accepting uploads.

## Network and reverse proxy

For agents connecting from elsewhere, set the agent address on the dashboard
([agent access](agent-access.md#turn-on-agent-access)) and forward the [agent endpoint and discovery paths](agent-access.md#with-a-reverse-proxy),
including the well-known paths at the origin root. Preserve the public Host header.
This optional interface does not support URL path prefixes.

The Compose file publishes Shijhon's port on every address of the machine, unencrypted,
for a network you trust. For access from outside, put a TLS reverse proxy in front:

1. Set `SHIJHON_BIND=127.0.0.1` in `.env`, so that Shijhon's port is published on this
   machine only. Otherwise a device that connects directly could appear to come from the
   proxy's address, and would be trusted.
2. List the proxy's address, and nothing else, in `[server] trusted_proxies`. For a proxy
   on the host, the configuration has the line, with the gateway of Shijhon's network
   (`.1` of `SHIJHON_NETWORK` in `.env`). Never list an address apps connect from directly.
3. Have the proxy append the client's address to `X-Forwarded-For` (`X-Real-IP` is not
   read) and send `X-Forwarded-Proto: https`, which marks the dashboard's cookie `Secure`.
4. Keep credentials out of the proxy's access log: apps send them in the query string
   (`p`, `t`, `s`), and Navidrome's web interface in the `X-ND-Authorization` header.

Navidrome limits failed logins per client address, so it has to learn each client's
address ([how](reference/advanced-deployment.md#client-addresses)).

## Catalog adapters

An adapter is installed into the image when it is built: a published one by a line in
`packaging/.env` and a rebuild ([catalogs.md](catalogs.md#installing-an-adapter)), one
that is not published from its wheel (`uv build --wheel` in the adapter's project), with
this command in the checkout:

```sh
docker build --build-context adapters=/path/to/wheels -t shijhon .
```

An adapter is code that runs inside Shijhon, with everything Shijhon reads: review it, and
the dependencies it brings, before you install it
([in detail](reference/advanced-deployment.md#catalog-adapters-in-detail)).

## The usage export

The cleanup has to know whether any user ever used a release. That is in Navidrome's
database, alongside its secrets and its users' passwords, so Shijhon's container does not
mount it: the `usage-export` service, which has no network access, writes a small file
with only what the cleanup reads
([what it holds](privacy-and-security.md#what-the-cleanup-reads)). The Compose file sets
it up; `[navidrome] usage_export_path` names the export.

To switch the cleanup on ([what it does](how-it-works.md#the-cleanup)), have the usage
export running early, since every daily check notes the uses it sees, even while the
cleanup is off. Look at what it would take out (`shijhon cleanup`, or "Run a dry run now"
on the Cleanup page), then set `[cleanup] mode = "on"` in the file or on that page.

## Operating

### Where the data is

The Compose file keeps the data in three Docker volumes, named with the Compose project's
name in front (`docker volume ls`): `shijhon_navidrome-data` (Navidrome's database and
cache), `shijhon_shijhon-data` (Shijhon's `state_dir`: its database, the snapshots taken
before upgrades, backups made with `shijhon backup`, downloads under way, the cover cache
and DASH songs) and `shijhon_usage-export`. `docker compose down` keeps them;
`docker compose down -v` deletes them. A one-shot `data-owner` service gives new
volumes to the user the containers run as at each start. Shijhon builds its image with
the project's name, `shijhon:local` by default, so that each installation on a machine
has its own.

To keep one in a folder instead, give its absolute path in `.env`: `NAVIDROME_DATA`,
`SHIJHON_DATA` or `USAGE_EXPORT_DATA`. Not with Docker Desktop, where a database in a
folder can be corrupted
([moving data into a volume](reference/advanced-deployment.md#moving-data-into-a-volume)).

### Backups

`shijhon backup` makes a backup of Shijhon's own state, also while Shijhon runs or after it
stopped. It writes
a folder named `shijhon-backup-` and the date and time (in UTC) into `backups` in
Shijhon's volume. The folder holds a copy of the database, made with SQLite's online
backup so that it is consistent even while Shijhon writes, and checked with SQLite's
integrity check. Beside it are the configuration file, the service account's password
file and a `README.txt` that says what the backup holds, what it leaves out and with which
version it was made. The command prints the same. When the configuration file or the
password file cannot be read, it makes no backup and says which file. The database holds
the settings saved on the dashboard, secrets included, so the folder and its files are
readable by their owner only. Keep the backup private, outside the checkout.

Run the command as the user Shijhon runs as (in the container it is). That user must be
able to write to the database's folder, also when the backup goes elsewhere: SQLite makes
the files it needs to read a database in WAL mode consistently beside the database.

From `packaging/`, make a backup and copy it out of the volume, with the folder name the
command printed:

```sh
docker compose exec shijhon shijhon --config /config/shijhon.toml backup
mkdir -p ~/shijhon-backups && chmod 700 ~/shijhon-backups
docker compose cp shijhon:/data/backups/shijhon-backup-20261112T153000Z ~/shijhon-backups/
docker compose exec shijhon rm -r /data/backups/shijhon-backup-20261112T153000Z
```

Backups stay in the volume until you remove them, as the last line does once the copy is
out. With Shijhon stopped, `docker compose run --rm --no-deps shijhon --config
/config/shijhon.toml backup` makes one the same way.

The backup is not a copy of the whole installation. Back these up as well:

- Navidrome's database. Navidrome makes its own backups, for example with
  `ND_BACKUP_PATH=/backups`, `ND_BACKUP_SCHEDULE="30 0 * * *"` and `ND_BACKUP_COUNT=14`, on
  backed-up storage.
- Your music, as you already back it up, and the placeholder folder in it.
- With music imports enabled, the entire dedicated imports directory, including hidden
  staging and retained hardlinks. Removed import batches still contain original music and
  are not disposable cache. Recover into the existing dedicated root so its device/inode
  binding stays intact; a replacement root must be reviewed rather than rewriting its
  marker or bypassing the guard.
- `.env`, and any other place where settings come from the environment.

Shijhon's records must match the placeholder folder, Navidrome's database and, with **Also
fix files in your music folder** on, the artist tags Shijhon changed in your music folder.
For a set to restore together, stop Shijhon, make the backup with `docker compose run` as
above, save those folders and Navidrome's database at the same time, and start Shijhon
again ([restoring](reference/advanced-deployment.md#restoring-a-backup)).

### Restarting

**Restart Shijhon**, on the Diagnostics page and wherever a saved setting waits for a
restart, shuts Shijhon down cleanly, and Docker or your service manager starts it again.
It stops nothing if the configuration file has an error or names another address or port.
Requests still open get 5 seconds: a song that is playing may have to be started again.

The Compose file's `restart: unless-stopped` starts it again; **a container run without a
restart policy stays stopped after you press it.** Outside a container, set `[server]
restart_by_supervisor = true` under a service manager that restarts Shijhon after an exit
([in detail](reference/advanced-deployment.md#restarting-and-stopping)).

### Upgrades

- Read [CHANGELOG.md](../CHANGELOG.md) first. Between beta versions the configuration can
  change.
- Shijhon's database migrates itself at startup, and an older Shijhon does not start on a
  database a newer one migrated. Before the first migration, the start writes a snapshot of
  the database (below), so you do not need to back it up by hand first.
- Build the new image and start it: `docker compose up -d --build` in `packaging/`. The
  line in `.env` installs its adapters again; a folder of wheels has to be given again.
- Stay on the Navidrome version the Shijhon release is tested with.
- Upgrading from 0.1.0b1: apps show each account's **All**
  ([Your collection](collection.md)): catalog albums that 0.1.0b1 added only because a
  song was played or put in an app's playlist are in it again once the account plays
  them or acts on them, and **Add whole album** on the Collection page adds them.

Before a new version migrates the database, its first start writes a copy of it into
`snapshots` in Shijhon's volume, checked like a backup. Its name gives the schema it
holds, the time (in UTC) and a number that counts snapshots up, for example
`shijhon-schema37-20261112T153000Z-4.sqlite3`. A new installation needs none. Only once
the new snapshot is complete are older ones removed: Shijhon keeps the newest snapshot of
the schema it upgrades from and the newest of the highest other schema, which is normally
the one the previous upgrade took. A start that is tried again on the same schema replaces
that schema's snapshot with a newer one, never the previous upgrade's. After a restore of
an older snapshot, the highest other schema can be a newer one than the restored
database's.

When the snapshot cannot be written, because there is not enough free space, the folder
cannot be written or the database is damaged, Shijhon upgrades the database and starts
without it, as versions before snapshots did. It removes no snapshot to make room, and it
leaves nothing half written. Its log says why (`docker compose logs shijhon` in `packaging/`), with how
much space the snapshot needed. Administrators see a line at the top of every dashboard
page that says from which schema to which the database was upgraded, when, and why, until
one of them dismisses it, and when notification addresses are set, it is reported once
([notifications](configuration.md#notifications)). Free some space, or make the folder
writable, so that the next upgrade can take its snapshot, and make a backup now with
`shijhon backup` ([backups](#backups)). When the reason is a
database that did not pass SQLite's integrity check, the line says so first: the database
may be damaged, and `shijhon backup` refuses to copy it until it passes, so restore an
earlier backup if you have one. `upgrade_snapshots = false` in the configuration (or
`SHIJHON_UPGRADE_SNAPSHOTS=false`) migrates without a snapshot, and then nothing is shown or
reported.

To go back, stop Shijhon, restore the snapshot and what changed with it, and only then
start the earlier version ([restoring](reference/advanced-deployment.md#restoring-a-backup)). Work done after the
upgrade is lost then. The snapshot holds only Shijhon's database: keep the earlier
configuration if you change it for the new version, and with music imports enabled back up
the imports directory before upgrading.

The first start at schema 24 also lists the catalog albums already in the library that no
account's records explain (many were added before Shijhon recorded who caused them); they
are in no account's library. To give them to one account's Shijhon library (and so its
**All**), run `shijhon carry-over --account <name>` once after that start, with the
account's Navidrome user name. It can run while Shijhon runs; other accounts never get them
(`--replace` gives them to another account instead), and `shijhon carry-over` alone shows
them and who has them.

Schema 33 adds `collection_revisions`, counters kept by triggers on the tables that decide
what a library selection holds, and an index for checking covers. The first start applies
it in a moment even on a large library. Older versions refuse this schema, so going back
means the coordinated recovery below. `collection-queries/` beside the database is a cache:
its old lists are replaced as they are used.

This release's database is at schema 45. Up to schema 31, playlists, collections, music
imports and agent access add tables, discovery mixes remember their creation requests,
agents' playlist requests keep the permissions they were made with, discarded import
uploads are deleted through recorded steps, placeholders and track links gain release
indexes, and cached album matches gain a matching revision and an owned-metadata
fingerprint, so existing negative matches are checked again when an album is next
considered by a view or the library pass. Schema 32 stores each add-on's fixed key, so
that renaming an add-on keeps its catalog. Schema 33 is described above. Schemas 34 and
35 turn the add-ons' routing modes into the add-on plan, with its steps and step times.
Schema 36 records the artist pictures Shijhon sets, schema 37 the artist lists it
finds and writes, with what undoes them, schema 38 which agent started a discovery mix
or a playlist import, so that revoking the agent stops that work. Schemas 39 to 45 add
the states that notifications report, the artists and releases of the accounts that follow
new releases, when each added release's track list was last read, the additions a daily
cap counts, the albums filled from another release with the song IDs of the release they
had, the albums agents were asked to add, and the record of an upgrade that went on
without its snapshot. The first start's
snapshot keeps Shijhon's database from before the upgrade. Keep a backup of Navidrome's
database from the same time and, with music imports enabled, the complete imports
directory, including private retained originals. Older application versions refuse the
newer schema; rollback requires coordinated recovery and review of activity since the
backup ([restoring a backup](reference/advanced-deployment.md#restoring-a-backup)). To
undo an import's removal, use its **Restore** instead. A pre-upgrade backup is a rollback
option only before newer library, playlist, import or history activity; after that,
reconcile the current data instead of restoring an older snapshot over it.

On first start, import jobs are rebound using their verified library and imports root,
including jobs blocked by earlier limit or local mount-path changes. Frozen reviews
remain approvable. Migration `0029_import_received_cleanup.sql` records deletion of
unpublished uploads; migrations 0030–0031 record cleanup refusals and visible destination
names. Startup finishes pending deletions; the worker cleans other eligible stopped or
finished jobs. Refused files retain their reservation and a recorded reason.

Before upgrading, stop Shijhon and back up the entire imports root; the first start's
snapshot holds the database that matches it. Deleted unpublished uploads require that
backup to recover. To roll back before newer activity, restore the paired backup and its
previous settings; older versions cannot use the converted bindings or newer schema.

### Docker Desktop on macOS and Windows

For a first look only; Shijhon is meant to run on a Linux host. Keep the data in Docker
volumes, the default: in a folder of the Mac or Windows disk, Navidrome's database can be
corrupted, and a Navidrome account created while the server runs is lost. Every device
that connects directly reaches Shijhon from one address, so one that keeps sending an old
password keeps that user out on all of them, 20 seconds at a time.
