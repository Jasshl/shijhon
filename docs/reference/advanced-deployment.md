# Advanced deployment

## The image

```sh
docker build -t shijhon .                                     # this machine's platform
docker buildx build --platform linux/amd64 --load -t shijhon:amd64 .   # for another one
```

The image runs as a non-root user, needs no capabilities and works with a read-only root
filesystem and a temporary `/tmp`. The Compose file builds it as `shijhon:local`.

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
  changed, `docker compose build --no-cache shijhon`, then `docker compose up -d`.

## Client addresses

Navidrome 0.64.2 limits failed logins: on `/rest`, 5 within 20 seconds per client address
and user (that user is then refused from that address, the right password too, for the 20
seconds); at `/auth/login`, 5 per 20 seconds per address. Navidrome learns each client's
address from Shijhon, and Shijhon from the proxy. Where it does not, all clients count as
one: a device with an old password locks that user's other devices.

- **Navidrome trusts Shijhon, and only Shijhon**: `ND_EXTAUTH_TRUSTEDSOURCES` holds
  Shijhon's address as a network, `172.30.99.10/32` in the Compose file (a bare address
  matches nothing; no space after a comma). With both on one host over loopback it is
  `127.0.0.1/32`, and every local process is then trusted, so keep Navidrome's port to
  yourself.
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
`USAGE_EXPORT_DATA=/srv/data/usage`):

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

From a folder named in `.env`: run `docker compose down`, take the folder's line out of
`.env`, then, with the volume's name and your `PUID:PGID`:

```sh
docker compose up --no-start &&
docker run --rm -v shijhon_navidrome-data:/data -v /path/to/folder:/from:ro alpine \
    sh -c 'cp -a /from/. /data/ && chown -R 1000:1000 /data' &&
docker compose up -d
```

## Restoring a backup

With everything stopped, from `packaging/`; then restore the placeholder folder and the
configuration of the same backup, and `docker compose start`:

```sh
docker compose stop &&
docker run --rm -v shijhon_shijhon-data:/data -v ~/shijhon-backups:/backup:ro alpine \
    sh -c 'tar -tzf /backup/shijhon-data.tar.gz >/dev/null &&
        find /data -mindepth 1 -delete && tar -xzf /backup/shijhon-data.tar.gz -C /data'
```

If it fails, start nothing: an unreadable archive leaves the volume as it was, a failure
while unpacking leaves part of it. Into an installation with another `PUID:PGID`, add
`&& chown -R <PUID>:<PGID> /data` at the end of the quoted command.

## Undoing automatic fills

`shijhon fills-undo` lists the automatic fills the fill policy would not make now; with
`--apply` it undoes those nobody ever used, by the cleanup's rules. It writes into the
library, so it works only with Shijhon stopped:

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
