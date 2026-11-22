# Advanced deployment

## The image

```sh
docker build -t shijhon .                                     # this machine's platform
docker buildx build --platform linux/amd64 --load -t shijhon:amd64 .   # for another one
```

The image runs as a non-root user, needs no capabilities and works with a read-only root
filesystem and a temporary `/tmp`. The Compose file builds it with the Compose project's
name, as `shijhon:local` by default, or `<name>:local` with another `COMPOSE_PROJECT_NAME`,
so that each installation on a machine has its own image. A one-shot `data-owner` service
gives new volumes to the user the containers run as at each start.

## Catalog adapters in detail

The `.env` line is the build argument `ADAPTERS`: requirement specifiers separated by
spaces, each the URL of a source archive or a wheel, or a package's name on the Python
package index (`name==1.2`). A folder of wheels, with Compose, goes in the `shijhon`
service's build:

```yaml
    build:
      context: ..
      args:
        ADAPTERS: ${SHIJHON_ADAPTERS:-}
      additional_contexts:
        adapters: /path/to/wheels
```

- The build installs the adapters with Shijhon and its dependencies held at their
  versions, and checks that Shijhon can load each. It stops with the reason when an
  address does not answer, an adapter needs other versions, a source archive needs a
  compiler or Git, one adapter is given twice, or nothing installed is an adapter.
- A source archive's build runs during the image build. The image and Docker's build
  records keep the address: give none that holds a password or a token, and install such
  an adapter from its wheel.
- Docker reuses the step while the line stays the same. When what an address serves has
  changed, run `docker compose build --no-cache shijhon`, then `docker compose up -d`, in
  `packaging/`.

## Client addresses

Navidrome 0.64.2 limits failed logins: on `/rest`, 5 within 20 seconds per client address
and user (that user is then refused from that address, the right password too, for the 20
seconds); at `/auth/login`, 5 per 20 seconds per address. Navidrome learns each client's
address from Shijhon, and Shijhon from the proxy. Where it does not, all clients count as
one: a device with an old password locks that user's other devices.

- **Navidrome trusts Shijhon, and only Shijhon**: `ND_EXTAUTH_TRUSTEDSOURCES` holds
  Shijhon's address as a network, `.10` of `SHIJHON_NETWORK` in the Compose file
  (`172.30.99.10/32` by default; a bare address matches nothing; no space after a comma).
  For a Navidrome you run yourself, the address depends on where it runs
  ([deployment](../deployment.md#your-navidrome-trusts-shijhon)). With both on one host over loopback it is
  `127.0.0.1/32`, and every local process is then trusted, so keep Navidrome's port to
  yourself. Or put Navidrome on a Unix socket that only Shijhon can open (Navidrome's
  `Address = "unix:<path>"`, Shijhon's `[navidrome] socket`) and trust only it with
  `"@"`, as the NixOS service does.
- **Shijhon names the client** in `X-Forwarded-For` and `X-Real-IP`, and drops what a
  client sent itself (`X-Forwarded-For`, `X-Real-IP`, `True-Client-IP`, `Forwarded`). The
  client's address is the connection's or, from a trusted proxy, the last address in
  `X-Forwarded-For` that is no trusted proxy.
- Navidrome 0.64.2 lists such players without an address, as behind any trusted proxy.
- Credentials an app sends in a POST form are read before Shijhon checks them, a form of
  up to 10 MiB; a request-size limit at the reverse proxy bounds that.

### Sign-in by the reverse proxy

Trusting Shijhon makes Navidrome accept a user header from it (`Remote-User`, its
`ExtAuth.UserHeader`) as the signed-in user, without a password.

- Shijhon drops `Remote-User`, and the header named in `[server]
  reverse_proxy_user_header`, from every request. **If Navidrome's `ExtAuth.UserHeader` is
  not `Remote-User`, set `reverse_proxy_user_header` to that name**, or an app could send
  it and be that user. Shijhon also reads the name from Navidrome's configuration where
  Navidrome shows it (`DevUIShowConfig`); when it cannot, the log says so.
- To let a proxy in front sign users in with that header, set `[server] reverse_proxy_auth
  = true`. The header is then accepted from `trusted_proxies` only: that must be exactly
  that proxy, Shijhon's port must be reachable only through it, and the proxy must replace
  a client's own header.
- When Navidrome cannot say whether credentials are good, requests are answered with HTTP
  502 and the log says why.

## The usage export in detail

The `usage-export` service runs Shijhon's image with no network access, with Navidrome's
data read-only and the `usage-export` volume writable. It exports every 15 minutes, and
when Shijhon asks by writing a request file into that volume, the only one Shijhon mounts.
The cleanup acts only on an export made after it asked. When none comes, the daily check
waits up to an hour and tries again within the hour, a dry run waits 90 seconds, and a
removal waits ten minutes, holding the requests for its release, then puts the release
back. An export of another database (a copy, an old backup) takes nothing out, and the
Cleanup page says why.

### From a systemd timer

Instead of the `usage-export` service (not both for one file), a timer on the host can run
one export a minute. Here the data is in folders (`NAVIDROME_DATA=/srv/data/navidrome`,
`USAGE_EXPORT_DATA=/srv/data/usage`), the containers run as 1000:1000 (your `PUID:PGID`)
and the image is `shijhon:local` (`<name>:local` with another `COMPOSE_PROJECT_NAME`):

```ini
# /etc/systemd/system/shijhon-usage-export.service
[Service]
Type=oneshot
ExecStart=/usr/bin/docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL \
    --user 1000:1000 -v /srv/data/navidrome:/navidrome-data:ro -v /srv/data/usage:/usage \
    shijhon:local usage-export --navidrome-db /navidrome-data/navidrome.db \
    --out /usage/usage.sqlite3

# /etc/systemd/system/shijhon-usage-export.timer
[Timer]
OnCalendar=minutely

[Install]
WantedBy=timers.target
```

Enable it with `systemctl enable --now shijhon-usage-export.timer`. A check then waits up
to a minute for each export; a path unit on `/srv/data/usage/usage.sqlite3.request`
(`PathChanged=`) that starts the same service makes that seconds.

### Reading Navidrome's database directly

Leave `usage_export_path` out, set `[navidrome] database_path =
"/navidrome-data/navidrome.db"` and mount Navidrome's data read-only into Shijhon's
container (`- ${NAVIDROME_DATA:-navidrome-data}:/navidrome-data:ro`). Nothing then waits
for an export, but Shijhon's container can read Navidrome's users, password data and
secrets. With both settings set, the export is read, with a warning at startup. Either
way, giving Navidrome its own `ND_PASSWORDENCRYPTIONKEY` (kept safe: without it nobody can
log in) means its database by itself does not give the passwords away.

## Music imports in detail

The overview is in [the deployment guide](../deployment.md#music-imports).

Compose and `./setup.sh` use the file set in `COMPOSE_FILE` on later runs. If an existing
project was started with `-f` instead, setup reads its containers' Compose file set and
saves it in `packaging/.env` when no `COMPOSE_FILE` is specified. An explicit
`COMPOSE_FILE` in the shell or `.env` takes precedence; keep the intended set in `.env` for
future commands. The overlay refuses a missing host directory, exposes published originals
read-only to Navidrome and enables imports in Shijhon.

## Restarting and stopping

**Restart Shijhon** reads the configuration file as the next start will and stops nothing
if it has an error (the page names the setting). It then finishes new tracks being
written, for up to 30 seconds, takes no new connections, gives open requests 5 seconds,
and exits with status 75; the dashboard waits and comes back once Shijhon answers. A start
can still fail on something else, such as a password file that cannot be read: the log
says why.

- It is offered where Shijhon is a container's first process, or with `[server]
  restart_by_supervisor = true`; otherwise Diagnostics shows the command to use.
- A restart that has not ended a minute later is ended as by a kill. A stop signal during
  a restart makes it an ordinary stop.

The Compose file gives Shijhon 30 seconds to stop (`stop_grace_period`; Docker's default is
10), so that a library write under way can finish; give it the same under any other
supervisor (systemd: `TimeoutStopSec=30`). What a kill leaves half written is repaired at
the next start, but an action under way may have to be made again.

The running Shijhon holds a lock in `state_dir`, so a second one on the same state does
not start. `state_dir` must be on a local file system: on a network file system, or in a
host folder on Docker Desktop, nothing refuses a second writer.

## Moving data into a volume

From a folder named in `.env`: run `docker compose down` in `packaging/`, take the
folder's line out of `.env`, then, also in `packaging/`, copy the folder into the volume
and give it to your `PUID:PGID` (on macOS usually `501:20`, which `.env` has). The volume
is named for the Compose project, `shijhon_navidrome-data` by default; `docker volume ls`
lists them. `docker compose up --no-start` creates the volumes and containers without
starting them (and builds the image if there is none yet). For Navidrome's data:

```sh
docker compose up --no-start &&
docker run --rm -v shijhon_navidrome-data:/data -v /path/to/folder:/from:ro alpine \
    sh -c 'cp -a /from/. /data/ && chown -R 501:20 /data' &&
docker compose up -d
```

Put your volume's name in place of `shijhon_navidrome-data`, and the `PUID:PGID` of `.env`
in place of `501:20`.

## Upgrades in detail

The overview is in [the deployment guide](../deployment.md#upgrades).

### Snapshots before an upgrade

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
([notifications](../configuration.md#notifications)). Free some space, or make the folder
writable, so that the next upgrade can take its snapshot, and make a backup now with
`shijhon backup` ([backups](../deployment.md#backups)). When the reason is a
database that did not pass SQLite's integrity check, the line says so first: the database
may be damaged, and `shijhon backup` refuses to copy it until it passes, so restore an
earlier backup if you have one. `upgrade_snapshots = false` in the configuration (or
`SHIJHON_UPGRADE_SNAPSHOTS=false`) migrates without a snapshot, and then nothing is shown or
reported.

### What each schema changed

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
35 turn the add-ons' earlier routing settings into the add-on plan, with its steps and step times.
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
backup ([restoring a backup](advanced-deployment.md#restoring-a-backup)). To
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

Upgrading from 0.1.0b1: apps show each account's **All**
  ([Your collection](../collection.md)): catalog albums that 0.1.0b1 added only because a
  song was played or put in an app's playlist are in it again once the account plays
  them or acts on them, and **Add whole album** on the Collection page adds them.

## Backups in detail

The overview is in [the deployment guide](../deployment.md#backups).

`shijhon backup` copies the database with SQLite's online backup, so that the copy is
consistent even while Shijhon writes, and checks it with SQLite's integrity check. Run the
command as the user Shijhon runs as (in the container it is). That user must be able to
write to the database's folder, also when the backup goes elsewhere: SQLite makes the files
it needs to read a database in WAL mode consistently beside the database. The command
refuses to run as root on a database another user owns.

## Restoring a backup

A snapshot taken before an upgrade and a backup made with `shijhon backup` are restored the
same way. A snapshot belongs to the version before the upgrade. A backup belongs to the
version its `README.txt` names; a later version migrates it again, after a new snapshot
when one can be written ([upgrades](../deployment.md#upgrades)).
Neither holds Navidrome's database, the placeholder folder or the imports directory, so
restore those from the same time too, when they changed since.

Do every step before you start anything again:

1. Stop everything, from `packaging/`: `docker compose stop`. With
   [music imports](../music-imports.md), run this and the commands below with both Compose
   files (`-f compose.yaml -f compose.imports.yaml`) unless `COMPOSE_FILE` in `.env` names
   them.

2. Put the copy of Shijhon's database in place. The commands copy it beside the database
   under a temporary name first, so a copy that fails leaves the working database as it
   is. Only then do they remove the database's `-wal` and `-shm` files, which belong to the
   database being replaced (SQLite would apply them to the restored copy and damage it),
   and rename the copy. For a snapshot in Shijhon's volume, with its name:

   ```sh
   docker compose run --rm --no-deps --entrypoint sh shijhon -c 'cd /data &&
       install -m 600 snapshots/shijhon-schema37-20261112T153000Z-4.sqlite3 restored.sqlite3 &&
       rm -f shijhon.sqlite3-wal shijhon.sqlite3-shm && mv restored.sqlite3 shijhon.sqlite3'
   ```

   For a backup you copied out of the volume, give the container its folder, which must be
   readable by your `PUID` (it is when you copied it out as that user):

   ```sh
   docker compose run --rm --no-deps -v ~/shijhon-backups:/backups:ro --entrypoint sh \
       shijhon -c 'cd /data &&
       install -m 600 /backups/shijhon-backup-20261112T153000Z/shijhon.sqlite3 restored.sqlite3 &&
       rm -f shijhon.sqlite3-wal shijhon.sqlite3-shm && mv restored.sqlite3 shijhon.sqlite3'
   ```

   When the configuration or the service account's password changed since, put the
   backup's `shijhon.toml` and `navidrome-password` back into `config/` too.

3. Restore what goes with it, from the same time. Restore Navidrome's database, so its song
   IDs and playlists match Shijhon's records, and the placeholder folder (`_shijhon` in your
   music folder), so its files match the placeholders Shijhon has records of. With music
   imports, restore the imports directory's contents into the existing directory. Do not
   delete, recreate or move the directory itself: Shijhon then refuses it ("The retained
   import root changed"). With **Also fix files in your music folder** on, artist lists
   written to files or undone since then count too: restore the music folder, or Shijhon
   can no longer undo the changes it made to those files.

4. Start again. To go back to an earlier version, check out its tag and build it
   (`docker compose up -d --build`); otherwise `docker compose start`.

Restore an older backup or snapshot only if nothing was played, added, imported or
published since it was taken; otherwise later work is lost or no longer matches the files.
For a snapshot that means going back soon after the upgrade.

An archive of the whole volume, made with Shijhon stopped as earlier versions of the
[backup guide](../deployment.md#backups) described, takes the place of step 2. The volume
is `shijhon_shijhon-data`, or starts with your `COMPOSE_PROJECT_NAME`:

```sh
docker run --rm -v shijhon_shijhon-data:/data -v ~/shijhon-backups:/backup:ro alpine \
    sh -c 'tar -tzf /backup/shijhon-data.tar.gz >/dev/null &&
        find /data -mindepth 1 -delete && tar -xzf /backup/shijhon-data.tar.gz -C /data'
```

If it fails, start nothing: an unreadable archive leaves the volume as it was, a failure
while unpacking leaves part of it. Into an installation with another `PUID:PGID`, add
`&& chown -R <PUID>:<PGID> /data` at the end of the quoted command, with the two numbers
from `.env`.

## Undoing automatic fills

`shijhon fills-undo` lists the automatic fills the fill policy would not make now; with
`--apply` it undoes those nobody ever used, by the cleanup's rules. It writes into the
library, so it works only with Shijhon stopped. From `packaging/`:

```sh
docker compose run --rm shijhon --config /config/shijhon.toml fills-undo   # the list, and its digest
docker compose stop shijhon
docker compose run --rm shijhon --config /config/shijhon.toml fills-undo --apply --expect <the digest>
docker compose start shijhon
```

Keep the `usage-export` service running meanwhile. After a stop that was not clean, start
Shijhon once first: the command runs no startup repairs, and refuses until they are done.
An undone fill's old IDs still play, and a use of one fills the album again.

## Hiding placeholders from other programs

Give Navidrome a **read-only union** of a folder that holds only `_shijhon` and the music
folder. With mergerfs, for example:

```
mergerfs -o ro,allow_other,cache.files=off,cache.attr=0,cache.entry=0,func.getattr=newest \
    /srv/placeholders=RO:/srv/music=RO /srv/library
```

- Navidrome and Shijhon both mount `/srv/library` as `/music`, read-only; Shijhon also
  mounts `/srv/placeholders/_shijhon` over `/music/_shijhon`, writable.
- Keep mergerfs's attribute and entry caches off, so that what Shijhon writes is visible
  at once.
- A union mount passes on no file-change notifications: give Navidrome a scan schedule
  (`ND_SCANNER_SCHEDULE=@every 1h`).
- Keep the mount outside any path your backups walk, and monitor the mergerfs process: if
  it dies, every access fails, and Navidrome's next scan marks the whole library missing.
