# Nix and NixOS

Build the native Linux package with Nix flakes:

```sh
nix build .#shijhon
./result/bin/shijhon version
./result/bin/shijhon catalogs
nix flake check
```

`flake.lock` pins Nixpkgs and the Python builders. [uv2nix](https://pyproject-nix.github.io/uv2nix/)
installs the runtime versions from `uv.lock`; build tools are pinned by the Nix inputs.
The default package uses Python 3.12 and includes ffmpeg on the command's `PATH`.
Tests and the demo catalog are outside the runtime environment. Linux x86-64 and ARM64
are declared; build and service checks must be run on each deployment architecture.
Docker Compose remains available as the [portable installation](quick-start.md).

## NixOS service

The service needs systemd 257 or newer and kernel PID namespaces for `PrivatePIDs`.

Add Shijhon as a flake input, then import `shijhon.nixosModules.default` in your NixOS
configuration. Pin the input to the release or revision you want to run. For example,
with `shijhon` passed to your module from the flake's inputs:

```nix
{
  imports = [ shijhon.nixosModules.default ];
  services.shijhon = {
    enable = true;
    configDirectory = "/etc/shijhon";
    stateDirectory = "/var/lib/shijhon";
    libraryPath = "/srv/music";
    placeholderPath = "/srv/shijhon-placeholders";
    usageDirectory = "/var/lib/shijhon-usage";
    usageExport = {
      enable = true;
      navidromeDataDirectory = "/var/lib/navidrome";
    };
  };
}
```

Prepare the directories and private configuration before starting the service.
`configDirectory` must contain `shijhon.toml` and any secret files it references.
Use the [example configuration](../packaging/shijhon.example.toml), with these paths
as the service sees them:

| Host setting | Service path |
| --- | --- |
| `configDirectory` | `/config`, read-only |
| `stateDirectory` | `/data`, writable |
| `libraryPath` | `/music`, read-only |
| `placeholderPath` | `/music/_shijhon`, writable |
| optional `importsPath` | `/music/_imports`, writable retained originals |
| `usageDirectory` | `/usage`, writable for export requests |

For example, a password file is `/config/navidrome-password`, and the Navidrome address
is the loopback address of your existing instance. The module sets the state, library,
placeholder and usage paths, the loopback listener and supervisor restart setting through
environment overrides. `port` defaults to 8765; `placeholderFolder` defaults to `_shijhon`.
Keep inline secrets and secret-bearing configuration out of Nix expressions: Nix-store
files can be read by local users.

Directory options accept absolute paths with letters, digits, `_`, `.`, `/`, `@`, `+`
and `-`; choose mountpoints without spaces. The module creates a dedicated `shijhon`
user/group and the state directory. Give that
user access to the configuration, existing library and placeholder/usage directories.
With `usageExport.enable`, the exporter runs as this same `user` and `group`. Give it
read and directory-traversal access to `usageExport.navidromeDataDirectory`, including
`navidrome.db` and its WAL sidecars. This directory is normally private; grant access
through a shared group or ACL, including access to newly created sidecars. The module
binds it read-only for the exporter but does not grant filesystem permissions.
For an existing installation set `user`, `group` and `createUser = false` to preserve its
identity. Existing directory contents are never recursively chowned. The module creates
no library, union mount, Navidrome instance, reverse proxy or backup job.

To enable [music-file imports](music-imports.md), provision a dedicated existing
`importsPath` and give the service user write access. Navidrome must see that same
folder at `/music/_imports`; `importsFolder` can change the directory name.
The module enables uploads only when `importsPath` is set. It never creates or
recursively changes ownership of that folder. Back it up in full, including its
hidden marker and staging receipts, together with Shijhon's state. Keep it outside
disposable placeholder storage and any audio-cache eviction rules.

Navidrome stays at the [tested version](deployment.md#what-shijhon-needs-from-navidrome).
It must see the same library and placeholder files at `/music`; preserve that path and
the library ID. A read-only library union may be needed when placeholders live on a
separate disk. The service requires its data and library mounts before starting.

The proxy runs in a mount namespace containing its runtime closure and the explicit
binds above. A private PID namespace and a small init isolate processes and reap children.
Its systemd unit drops capabilities, forbids privilege escalation and gives
it a noexec temporary directory. The networkless `shijhon-usage-export` service alone
gets `/navidrome-data`, including the database's WAL sidecars, read-only. It exports at
startup, every 900 seconds by default and when Shijhon writes a request file. Prepare
the usage directory before enabling it; the proxy does not get Navidrome's database.

```sh
systemctl status shijhon shijhon-usage-export
systemctl restart shijhon
```

The dashboard's restart action exits through the normal graceful shutdown and systemd
starts it again. A service stop allows 30 seconds, then kills any remaining children.
Inspect logs locally without copying credentials or private addresses into issue reports.

Host-installed AI commands need explicit runtime mounts and compatible systemd sandbox
settings. The default namespace exposes no host home, login or command installation.
Keep the runner's own tool, filesystem and credential isolation when extending the unit;
an unrestricted host shell is unsuitable for this service.

## Catalog adapters

Build an adapter into the same Python environment with `lib.mkShijhon`. For an adapter
that provides an overlay for the pyproject.nix package set:

```nix
services.shijhon.package = shijhon.lib.mkShijhon {
  inherit pkgs;
  extraOverlays = [ adapter.overlays.default ];
  extraPackages = [ "shijhon-catalog-example" ];
};
```

`extraOverlays` adds package definitions; `extraPackages` selects the adapter roots to
install. Those roots and their dependencies are resolved in the same package set as
Shijhon. The environment build runs `uv pip check`, so incompatible requirements fail.
Keep Shijhon's locked dependency definitions when writing an adapter overlay; additional
dependencies need their own pinned sources and hashes. `python` can select another
compatible Nixpkgs interpreter. `pythonEnvironment` on the resulting package exposes the
complete interpreter environment for separately managed companion scripts.

An adapter is code running with Shijhon's access. Review its source, dependencies and
overlay before installing it. `shijhon catalogs` lists the installed adapters.

## Moving from a container

Preserve the existing UID/GID, database, state, placeholder folder, configuration and
Navidrome library ID. Keep the same paths inside the service as inside the old container;
the module's binds make `/data`, `/config`, `/music` and `/usage` available without moving
their host folders. Keep Navidrome's deployment and pinned version during this change.

Before switching, build the exact package with its adapters, check its CLI and a disposable
service using synthetic data, and record the old image and unit configuration. Arrange a
consistent backup of state, configuration, placeholders and, with music imports, the
imports directory, taken with the old service stopped. Never run both proxies against the
same state.

Account for companion processes too: move the usage exporter to the native unit or keep
the existing exporter until it is replaced; retain its request-file path. Preserve backup
timers, exclusions, mount ordering and proxy routing explicitly.

At cutover stop the old application writers, then start the native units. Verify startup
migrations, authenticated requests, library/playlist IDs and order, usage-export freshness,
graceful restart and any configured AI runner. Roll back the service configuration only
when the old version supports the current schema; restoring an older database over later
work needs a separate reviewed recovery procedure. Never restore a stale AI login file.
