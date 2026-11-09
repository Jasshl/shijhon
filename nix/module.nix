{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib)
    mkEnableOption
    mkForce
    mkIf
    mkMerge
    mkOption
    optional
    optionals
    types
    ;
  cfg = config.services.shijhon;
  nd = config.services.navidrome;
  managed = cfg.navidrome.manage;
  usage = cfg.usageExport.enable;
  # The Navidrome release Shijhon is tested with (docs/deployment.md).
  testedNavidrome = "0.64.2";
  navidromeVersion = lib.getVersion nd.package;
  # These paths also appear in systemd's whitespace/colon-separated mount directives.
  absolute = types.strMatching "/[A-Za-z0-9_][A-Za-z0-9_./@+-]*";
  settingsFormat = pkgs.formats.toml { };
  configFile = settingsFormat.generate "shijhon.toml" cfg.settings;
  placeholderDirectory =
    if cfg.placeholderPath != null then
      cfg.placeholderPath
    else
      "${cfg.libraryPath}/${cfg.placeholderFolder}";
  # A managed Navidrome listens only on a Unix socket in this folder, whose group only
  # Shijhon's two units get (SupplementaryGroups): no other local process reaches it, so
  # Navidrome can trust it to name each app's address.
  socketDirectory = "/run/shijhon-navidrome";
  socket = "${socketDirectory}/navidrome.sock";
  socketGroup = "shijhon-navidrome";
  runner = cfg.aiRunner;
  closureOf =
    extra:
    pkgs.closureInfo {
      rootPaths = [
        cfg.package
        pkgs.cacert
        pkgs.tini
        configFile
      ]
      ++ extra;
    };
  pathsOf =
    closure:
    lib.filter (s: s != "") (lib.splitString "\n" (builtins.readFile "${closure}/store-paths"));
  closure = closureOf [ ];
  runtimePaths = pathsOf closure;
  # Shijhon's own unit also holds the local AI runners (no other unit may run them).
  shijhonClosure = closureOf runner.packages;
  shijhonPaths = pathsOf shijhonClosure;
  shijhon = "${cfg.package}/bin/shijhon";
  certificates = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
  environment = {
    PYTHONDONTWRITEBYTECODE = "1";
    PYTHONUNBUFFERED = "1";
    SSL_CERT_FILE = certificates;
    NIX_SSL_CERT_FILE = certificates;
  };
  isolationWith = runtimePaths: {
    User = cfg.user;
    Group = cfg.group;
    # What Shijhon writes into the library is readable by Navidrome and other readers of
    # the library, as in the Docker setup. Its state directory stays 0700 and its secrets
    # 0600 whatever the mask.
    UMask = "0022";
    Restart = "on-failure";
    SuccessExitStatus = [ 143 ];
    RestartSec = 3;
    TimeoutStopSec = 30;
    # The init forwards TERM to the app, lets it drain writes and reaps orphan children.
    # On timeout systemd kills the entire cgroup.
    KillMode = "mixed";
    NoNewPrivileges = true;
    CapabilityBoundingSet = "";
    PrivateDevices = true;
    PrivatePIDs = true;
    ProtectHome = true;
    ProtectSystem = "strict";
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectControlGroups = true;
    RestrictSUIDSGID = true;
    RestrictNamespaces = true;
    RestrictAddressFamilies = [
      "AF_UNIX"
      "AF_INET"
      "AF_INET6"
    ];
    LockPersonality = true;
    # An empty mount namespace exposes only the runtime and explicit data binds.
    TemporaryFileSystem = [ "/tmp:rw,noexec,nosuid,nodev,mode=1777,size=256M" ];
    BindReadOnlyPaths = runtimePaths;
    # '+' resolves these restrictions inside RootDirectory, including bind targets.
    NoExecPaths = [ "+/" ];
    ExecPaths = map (path: "+${path}") runtimePaths;
  };
  isolation = isolationWith runtimePaths;
  # Run as root before Shijhon starts. The library must be there: nothing in it is made
  # or changed but the placeholder folder itself, which systemd-tmpfiles makes or
  # adjusts without following links, owned by Shijhon and Navidrome's group, setgid.
  library = pkgs.writeShellScript "shijhon-library" ''
    if [ ! -d ${lib.escapeShellArg cfg.libraryPath} ]; then
      echo "shijhon: the library folder ${cfg.libraryPath} does not exist" >&2
      exit 1
    fi
  '';
  placeholderRule = pkgs.writeText "shijhon-placeholders.conf" ''
    d ${placeholderDirectory} 2755 ${cfg.user} ${cfg.navidrome.group} - -
  '';
  # The person's password, for creating the account, copied where only the accounts
  # unit reads it; a file deleted once the account exists is no error.
  accountPasswordCopy = "/run/shijhon-secrets/account-password";
  copyPassword = pkgs.writeShellScript "shijhon-account-password" ''
    if [ -f ${lib.escapeShellArg account.passwordFile} ]; then
      ${pkgs.coreutils}/bin/install -m 0400 -o ${cfg.user} -g ${cfg.group} -T \
        ${lib.escapeShellArg account.passwordFile} ${accountPasswordCopy}
    else
      ${pkgs.coreutils}/bin/rm -f ${accountPasswordCopy}
    fi
  '';
  account = cfg.account;
  accountPassword = account != null && account.passwordFile != null;
  # Without a service-account password of the person's, shijhon-accounts creates the
  # service account (in a managed Navidrome, or through the person's admin account in
  # their own) and keeps its random password in the state directory.
  createsAccounts = cfg.navidrome.passwordFile == null;
  # The header Navidrome takes the signed-in user from when Shijhon's socket names one;
  # Shijhon drops it from apps' requests.
  userHeader = (cfg.settings.server or { }).reverse_proxy_user_header or "Remote-User";
  # Whether the managed Navidrome's setting is what Shijhon sets: a definition of the
  # person's that differs is a conflict (caught here) or, forced, another value.
  holds =
    get: value:
    let
      r = builtins.tryEval (get nd.settings);
    in
    r.success && r.value == value;
in
{
  options.services.shijhon = {
    enable = mkEnableOption "Shijhon";
    package = mkOption {
      type = types.package;
      description = "Shijhon package, including selected catalog adapters.";
    };
    libraryPath = mkOption {
      type = absolute;
      example = "/srv/music";
      description = "Your music library, an existing folder. Navidrome and Shijhon read it; Shijhon writes only inside its placeholder folder there.";
    };
    account = mkOption {
      type = types.nullOr (
        types.submodule {
          options = {
            name = mkOption {
              type = types.nonEmptyStr;
              description = "Your Navidrome user name. A Navidrome without accounts gets it as an admin, after Shijhon's service account. In a Navidrome that has accounts it must be an admin: Shijhon signs in with it once to create its service account. An existing account keeps its password.";
            };
            passwordFile = mkOption {
              type = types.nullOr absolute;
              default = null;
              description = "A file with the account's password, read only until Shijhon's service account and yours exist. Keep it outside the Nix store. Once they exist the file may go.";
            };
          };
        }
      );
      default = null;
      description = "Your own Navidrome account: created at the first start when Navidrome has no accounts, or, in a Navidrome that has accounts, the admin account that creates Shijhon's service account. With navidrome.manage = false, set it or navidrome.passwordFile.";
    };
    listenAddress = mkOption {
      type = types.str;
      default = "127.0.0.1";
      example = "0.0.0.0";
      description = "Address Shijhon listens on. The default reaches it from this machine only (and through a reverse proxy here); \"0.0.0.0\" or \"::\" lets apps on your network connect directly, unencrypted.";
    };
    port = mkOption {
      type = types.port;
      default = 4533;
      description = "Port apps connect to.";
    };
    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = "Open `port` in the firewall, for apps on your network.";
    };
    settings = mkOption {
      type = types.submodule { freeformType = settingsFormat.type; };
      default = { };
      example = {
        cleanup.mode = "dry_run";
        server.trusted_proxies = [ "127.0.0.0/8" ];
      };
      description = "Further settings of shijhon.toml (docs/configuration.md). They are written to the Nix store, readable by every local user: no secrets. The module sets the paths, the listener and the Navidrome connection itself.";
    };
    environmentFile = mkOption {
      type = types.nullOr absolute;
      default = null;
      description = "A file with SHIJHON_* settings that are secrets, such as SHIJHON_AUDIOMUSE__TOKEN, read by systemd at each start. Add-ons and the catalog are set on the dashboard.";
    };
    navidrome = {
      manage = mkOption {
        type = types.bool;
        default = true;
        description = "Set up NixOS's Navidrome service (services.navidrome, also one you run already) for Shijhon: on a Unix socket only Shijhon can open, with libraryPath as its music folder, and create its accounts at the first start. Off: use a Navidrome you run yourself (url, and account or passwordFile).";
      };
      allowUntestedVersion = mkOption {
        type = types.bool;
        default = false;
        description = "Allow a Navidrome version Shijhon has not been tested with (the build then warns instead of failing).";
      };
      url = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "http://127.0.0.1:4534";
        description = "Your own Navidrome's address, at the root (no BaseURL), reachable only from this machine. For one on a Unix socket, set settings.navidrome.socket too.";
      };
      user = mkOption {
        type = types.nonEmptyStr;
        default = "shijhon";
        description = "Shijhon's Navidrome service account: an admin account used only by Shijhon.";
      };
      passwordFile = mkOption {
        type = types.nullOr absolute;
        default = null;
        description = "With your own Navidrome and a service account you created: a file with its password. Without it, the service account is created at the first start, with a random password kept in stateDirectory.";
      };
      group = mkOption {
        type = types.str;
        default = if managed then nd.group else cfg.group;
        defaultText = lib.literalExpression "if navidrome.manage then config.services.navidrome.group else group";
        description = "The group Navidrome reads the library with. The placeholder folder belongs to it, so that Navidrome can read what Shijhon writes there.";
      };
    };
    user = mkOption {
      type = types.str;
      default = "shijhon";
      description = "Service user; created only when createUser is true.";
    };
    group = mkOption {
      type = types.str;
      default = "shijhon";
      description = "Service group; created only when createUser is true.";
    };
    createUser = mkOption {
      type = types.bool;
      default = true;
      description = "Create the service user and group. Disable to keep an existing identity.";
    };
    stateDirectory = mkOption {
      type = absolute;
      default = "/var/lib/shijhon";
      description = "Shijhon's database, caches and the managed Navidrome's service-account password. Created if absent; existing contents are never recursively chowned.";
    };
    placeholderFolder = mkOption {
      type = types.strMatching "[A-Za-z0-9_.-]+";
      default = "_shijhon";
      description = "Name of the placeholder folder inside the library.";
    };
    placeholderPath = mkOption {
      type = types.nullOr absolute;
      default = null;
      description = "Where the placeholder folder really is, when libraryPath is a read-only union you set up that shows it as <placeholderFolder>. Default: inside libraryPath.";
    };
    importsPath = mkOption {
      type = types.nullOr absolute;
      default = null;
      description = "An existing folder for music imports (retained originals), writable at <importsFolder> in the library; Navidrome must see it there. Set it to enable imports. Never inside the placeholder folder; back it up with Shijhon's state.";
    };
    importsFolder = mkOption {
      type = types.strMatching "[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}";
      default = "_imports";
      description = "Name of the imports folder inside the library.";
    };
    aiRunner = {
      packages = mkOption {
        type = types.listOf types.package;
        default = [ ];
        description = "Local AI runner commands for playlists (docs/playlists.md), made available inside Shijhon's sandbox and on its PATH.";
      };
      home = mkOption {
        type = types.nullOr absolute;
        default = null;
        example = "/var/lib/shijhon-runner";
        description = "A folder for the runners' sign-in: Shijhon's HOME inside the sandbox, writable, never executable. Created if absent, mode 0700.";
      };
    };
    usageExport = {
      enable = mkOption {
        type = types.bool;
        default = managed;
        defaultText = lib.literalExpression "navidrome.manage";
        description = "Run the networkless usage exporter, which tells the cleanup which releases are used without giving Shijhon Navidrome's database.";
      };
      navidromeDataDirectory = mkOption {
        type = types.nullOr absolute;
        default = if managed then nd.settings.DataFolder or "/var/lib/navidrome" else null;
        defaultText = lib.literalExpression "the managed Navidrome's data folder";
        description = "Navidrome's data folder, visible read-only to the exporter only.";
      };
      user = mkOption {
        type = types.str;
        default = if managed then nd.user else cfg.user;
        defaultText = lib.literalExpression "if navidrome.manage then config.services.navidrome.user else user";
        description = "The user the exporter runs as: one that can read Navidrome's database. It runs with Shijhon's group, so that Shijhon can read the export.";
      };
      directory = mkOption {
        type = absolute;
        default = "/var/lib/shijhon-usage";
        description = "Where the export is written; created if absent.";
      };
      everySeconds = mkOption {
        type = types.ints.positive;
        default = 900;
        description = "Export interval. Exports Shijhon asks for are also served.";
      };
    };
  };

  config = mkIf cfg.enable (mkMerge [
    {
      assertions = [
        {
          assertion =
            !lib.elem cfg.placeholderFolder [
              "."
              ".."
            ];
          message = "services.shijhon.placeholderFolder must name a child directory.";
        }
        {
          assertion =
            cfg.importsPath == null
            || (
              cfg.importsFolder != cfg.placeholderFolder
              && cfg.importsPath != placeholderDirectory
              && !lib.hasPrefix (placeholderDirectory + "/") cfg.importsPath
              && !lib.hasPrefix (cfg.importsPath + "/") placeholderDirectory
            );
          message = "services.shijhon: music imports and the placeholder folder need separate folders.";
        }
        {
          assertion = managed || cfg.navidrome.url != null;
          message = "services.shijhon: with navidrome.manage = false, set navidrome.url to your own Navidrome's address.";
        }
        {
          assertion = managed || (account == null) != createsAccounts;
          message = "services.shijhon: with navidrome.manage = false, set either account (an admin account your Navidrome has: Shijhon creates its service account with it) or navidrome.passwordFile (the password of a service account you created), not both.";
        }
        {
          assertion = !managed || (cfg.navidrome.url == null && cfg.navidrome.passwordFile == null);
          message = "services.shijhon: navidrome.url and navidrome.passwordFile are for your own Navidrome (navidrome.manage = false).";
        }
        {
          assertion = !usage || cfg.usageExport.navidromeDataDirectory != null;
          message = "services.shijhon.usageExport needs navidromeDataDirectory.";
        }
      ];
      warnings = optional (
        cfg.openFirewall
        && lib.elem cfg.listenAddress [
          "127.0.0.1"
          "::1"
          "localhost"
        ]
      ) "services.shijhon.openFirewall has no effect while listenAddress is ${cfg.listenAddress}.";

      services.shijhon.settings = mkMerge [
        {
          state_dir = "/data";
          server = {
            host = cfg.listenAddress;
            inherit (cfg) port;
            restart_by_supervisor = true;
          };
          navidrome = {
            # (With the socket: a name that resolves nowhere, only the Host header.)
            url = if managed then "http://navidrome.invalid" else cfg.navidrome.url;
            inherit (cfg.navidrome) user;
            password_file =
              if createsAccounts then
                "/data/navidrome-password"
              else
                "/run/credentials/shijhon.service/navidrome-password";
            library_path = "/music";
          };
          placeholders.folder = cfg.placeholderFolder;
        }
        (mkIf managed { navidrome.socket = socket; })
        (mkIf usage { navidrome.usage_export_path = "/usage/usage.sqlite3"; })
        (mkIf (cfg.importsPath != null) {
          music_imports = {
            enabled = true;
            folder = cfg.importsFolder;
          };
        })
      ];

      users.users = mkIf cfg.createUser {
        ${cfg.user} = {
          isSystemUser = true;
          group = cfg.group;
        };
      };
      users.groups = mkMerge [
        (mkIf cfg.createUser { ${cfg.group} = { }; })
        (mkIf managed { ${socketGroup} = { }; })
      ];
      networking.firewall.allowedTCPPorts = mkIf cfg.openFirewall [ cfg.port ];

      systemd.tmpfiles.rules = [
        "d '${cfg.stateDirectory}' 0700 ${cfg.user} ${cfg.group} - -"
        "d /run/shijhon-root 0755 root root - -"
      ]
      ++ optionals usage [
        # Shijhon writes its export requests here, the exporter the export.
        "d '${cfg.usageExport.directory}' 0770 ${cfg.user} ${cfg.group} - -"
        "d /run/shijhon-usage-root 0755 root root - -"
      ]
      ++ optional (runner.home != null) "d '${runner.home}' 0700 ${cfg.user} ${cfg.group} - -"
      ++ optional createsAccounts "d /run/shijhon-accounts-root 0755 root root - -"
      ++ optionals managed [
        # Navidrome makes its socket here (0660); the folder passes Shijhon's group on.
        "d ${socketDirectory} 2750 ${nd.user} ${socketGroup} - -"
      ];

      systemd.services.shijhon = {
        description = "Shijhon";
        wantedBy = [ "multi-user.target" ];
        wants = [ "network-online.target" ] ++ optional usage "shijhon-usage-export.service";
        requires = [
          "shijhon-prepare.service"
        ]
        ++ optional createsAccounts "shijhon-accounts.service";
        after = [
          "network-online.target"
          "shijhon-prepare.service"
        ]
        ++ optional nd.enable "navidrome.service"
        ++ optional createsAccounts "shijhon-accounts.service"
        ++ optional usage "shijhon-usage-export.service";
        unitConfig.RequiresMountsFor = [
          cfg.stateDirectory
          cfg.libraryPath
          placeholderDirectory
        ]
        ++ optional (cfg.importsPath != null) cfg.importsPath
        ++ optional usage cfg.usageExport.directory;
        restartTriggers = [
          cfg.package
          shijhonClosure
        ];
        environment = environment // lib.optionalAttrs (runner.home != null) { HOME = "/runner"; };
        path = runner.packages;
        serviceConfig = isolationWith shijhonPaths // {
          RootDirectory = "/run/shijhon-root";
          SupplementaryGroups = optional managed socketGroup;
          NoExecPaths =
            (isolationWith shijhonPaths).NoExecPaths
            ++ [
              "+/data"
              "+/music"
              "+/music/${cfg.placeholderFolder}"
            ]
            ++ optional (cfg.importsPath != null) "+/music/${cfg.importsFolder}"
            ++ optional usage "+/usage"
            ++ optional (runner.home != null) "+/runner";
          WorkingDirectory = "/data";
          ExecStart = lib.escapeShellArgs [
            "${pkgs.tini}/bin/tini"
            "--"
            shijhon
            "--config"
            configFile
            "serve"
          ];
          BindReadOnlyPaths =
            shijhonPaths
            ++ [
              "${cfg.libraryPath}:/music"
              "/etc/resolv.conf"
              "/etc/hosts"
              # For programs that look for CA certificates where most systems keep them.
              "${certificates}:/etc/ssl/certs/ca-certificates.crt"
            ]
            ++ optional managed socketDirectory;
          BindPaths = [
            "${cfg.stateDirectory}:/data"
            "${placeholderDirectory}:/music/${cfg.placeholderFolder}"
          ]
          ++ optional (cfg.importsPath != null) "${cfg.importsPath}:/music/${cfg.importsFolder}"
          ++ optional usage "${cfg.usageExport.directory}:/usage"
          ++ optional (runner.home != null) "${runner.home}:/runner";
          LoadCredential = optional (!createsAccounts) "navidrome-password:${cfg.navidrome.passwordFile}";
          EnvironmentFile = optional (cfg.environmentFile != null) cfg.environmentFile;
        };
      };

      # As root, before the sandboxes, whose binds need the folder to exist.
      systemd.services.shijhon-prepare = {
        description = "Shijhon's placeholder folder";
        before = [
          "shijhon.service"
          "shijhon-accounts.service"
        ];
        unitConfig.RequiresMountsFor = [
          cfg.libraryPath
          placeholderDirectory
        ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = [
            library
            "${config.systemd.package}/bin/systemd-tmpfiles --create ${placeholderRule}"
          ]
          ++ optional accountPassword copyPassword;
          RuntimeDirectory = optional accountPassword "shijhon-secrets";
          RuntimeDirectoryMode = "0700";
          UMask = "0077";
          # Only the placeholder folder's parent (and the runtime folder) is writable.
          ProtectSystem = "strict";
          ProtectHome = "read-only";
          ReadWritePaths = [ "-${dirOf placeholderDirectory}" ];
          PrivateTmp = true;
          PrivateDevices = true;
          PrivateNetwork = true;
          NoNewPrivileges = true;
          CapabilityBoundingSet = [
            "CAP_CHOWN"
            "CAP_FOWNER"
            "CAP_FSETID"
            "CAP_DAC_OVERRIDE"
            "CAP_DAC_READ_SEARCH"
          ];
          RestrictAddressFamilies = [ "AF_UNIX" ];
          RestrictNamespaces = true;
          ProtectKernelTunables = true;
          ProtectKernelModules = true;
          ProtectControlGroups = true;
          LockPersonality = true;
          SystemCallArchitectures = "native";
        };
      };

      systemd.services.shijhon-usage-export = mkIf usage {
        description = "Shijhon's Navidrome usage export";
        wantedBy = [ "multi-user.target" ];
        after = optional nd.enable "navidrome.service";
        unitConfig.RequiresMountsFor = [
          cfg.usageExport.navidromeDataDirectory
          cfg.usageExport.directory
        ];
        inherit environment;
        serviceConfig = isolation // {
          # Navidrome's user reads its database; Shijhon's group shares the export folder.
          User = cfg.usageExport.user;
          RootDirectory = "/run/shijhon-usage-root";
          NoExecPaths = isolation.NoExecPaths ++ [
            "+/navidrome-data"
            "+/usage"
          ];
          PrivateNetwork = true;
          ExecStart = lib.escapeShellArgs [
            "${pkgs.tini}/bin/tini"
            "--"
            shijhon
            "usage-export"
            "--navidrome-db"
            "/navidrome-data/navidrome.db"
            "--out"
            "/usage/usage.sqlite3"
            "--every"
            (toString cfg.usageExport.everySeconds)
          ];
          BindReadOnlyPaths = runtimePaths ++ [
            "${cfg.usageExport.navidromeDataDirectory}:/navidrome-data"
          ];
          BindPaths = [ "${cfg.usageExport.directory}:/usage" ];
        };
      };
    }

    (mkIf managed {
      assertions = [
        {
          assertion = cfg.navidrome.allowUntestedVersion || navidromeVersion == testedNavidrome;
          message = "services.shijhon: Shijhon is tested with Navidrome ${testedNavidrome}, and services.navidrome.package is Navidrome ${navidromeVersion}. Use a Nixpkgs with Navidrome ${testedNavidrome}, or set services.shijhon.navidrome.allowUntestedVersion = true to try this version.";
        }
        {
          assertion = lib.all (name: lib.toLower name != "baseurl" || nd.settings.${name} == "") (
            lib.attrNames nd.settings
          );
          message = "services.shijhon needs Navidrome at the root of its address: leave services.navidrome.settings.BaseUrl unset.";
        }
        {
          assertion = holds (s: s.Address) "unix:${socket}" && holds (s: s.UnixSocketPerm) "0660";
          message = "services.shijhon puts Navidrome on a Unix socket only Shijhon can open: remove services.navidrome.settings.Address (and Port and UnixSocketPerm) from your configuration. Apps connect to Shijhon, at services.shijhon.port.";
        }
        {
          assertion = holds (s: s.MusicFolder) cfg.libraryPath;
          message = "services.shijhon gives Navidrome services.shijhon.libraryPath as its music folder: remove services.navidrome.settings.MusicFolder from your configuration, and set libraryPath to that folder.";
        }
        {
          assertion = holds (s: s.ExtAuth.TrustedSources) "@";
          message = "services.shijhon has Navidrome trust only Shijhon's socket: remove services.navidrome.settings.ExtAuth.TrustedSources (and ReverseProxyWhitelist) from your configuration.";
        }
        {
          assertion = holds (s: s.ExtAuth.UserHeader) userHeader;
          message = "services.shijhon: Navidrome trusts Shijhon's socket to name users in its ExtAuth.UserHeader; set services.shijhon.settings.server.reverse_proxy_user_header to the same name, so that Shijhon drops it from apps' requests.";
        }
        {
          assertion = ((nd.settings.Scanner or { }).PurgeMissing or "never") == "never";
          message = "services.shijhon needs Navidrome's Scanner.PurgeMissing = \"never\" (its default): otherwise a placeholder that is gone for a moment loses its favorites and playlist entries.";
        }
      ];
      warnings =
        optional (cfg.navidrome.allowUntestedVersion && navidromeVersion != testedNavidrome)
          "services.shijhon: Navidrome ${navidromeVersion} has not been tested with Shijhon (tested: ${testedNavidrome})."
        ++ optional nd.openFirewall "services.navidrome.openFirewall opens a port Navidrome no longer listens on (it is on Shijhon's socket): remove it, and use services.shijhon.openFirewall for Shijhon's port.";

      services.navidrome = {
        enable = true;
        settings = {
          MusicFolder = cfg.libraryPath;
          # Only Shijhon's group can open the socket; Navidrome trusts it (and nothing
          # else) to name each app's address, so its login limits count apps apart.
          Address = "unix:${socket}";
          UnixSocketPerm = "0660";
          ExtAuth = {
            TrustedSources = "@";
            # Written out, so that Navidrome ignores a deprecated ReverseProxyUserHeader.
            UserHeader = lib.mkDefault userHeader;
          };
        };
      };
      systemd.services.navidrome.serviceConfig.BindPaths = [ socketDirectory ];
      # Navidrome's module would create a missing music folder as its own, private: the
      # library is the person's, and Shijhon reads it too. Its own rule is left out.
      systemd.tmpfiles.settings.navidromeDirs.${cfg.libraryPath} = mkForce { };
      systemd.services.navidrome.unitConfig.RequiresMountsFor = [
        cfg.libraryPath
        placeholderDirectory
      ];
    })

    # The service account, its random password kept in the state directory, and the
    # person's own: in a Navidrome without accounts as its first two, in one with
    # accounts through the person's admin account. Existing accounts are kept.
    (mkIf createsAccounts {
      systemd.services.shijhon-accounts = {
        description = "Shijhon's Navidrome accounts";
        wants = if managed then [ "navidrome.service" ] else [ "network-online.target" ];
        requires = [ "shijhon-prepare.service" ];
        after = [
          "shijhon-prepare.service"
        ]
        ++ optional nd.enable "navidrome.service"
        ++ optional (!managed) "network-online.target";
        before = [ "shijhon.service" ];
        unitConfig.RequiresMountsFor = [ cfg.stateDirectory ];
        inherit environment;
        serviceConfig = isolation // {
          Type = "oneshot";
          RemainAfterExit = true;
          SupplementaryGroups = optional managed socketGroup;
          # Again after Navidrome did not answer as it should (exit status 1), while
          # Shijhon's start waits for it (direct: no failed state in between, which would
          # fail that start); not after what needs changing first (2). A switch waits too.
          Restart = "on-failure";
          RestartMode = "direct";
          RestartSec = 10;
          RestartPreventExitStatus = [ 2 ];
          # (Its own wait is 180 s, each request at most 10 s more.)
          TimeoutStartSec = "5min";
          # A managed Navidrome is on its socket only.
          PrivateNetwork = managed;
          RestrictAddressFamilies = if managed then [ "AF_UNIX" ] else isolation.RestrictAddressFamilies;
          RootDirectory = "/run/shijhon-accounts-root";
          NoExecPaths = isolation.NoExecPaths ++ [ "+/data" ];
          WorkingDirectory = "/data";
          ExecStart = lib.escapeShellArgs (
            [
              shijhon
              "--config"
              configFile
              "navidrome-accounts"
              "--wait"
              "180"
              "--password-file-name"
              "${cfg.stateDirectory}/navidrome-password"
              "--account-setting-name"
              "services.shijhon.account.name"
              "--account-password-setting-name"
              "services.shijhon.account.passwordFile"
            ]
            ++ optionals (account != null) [
              "--account"
              account.name
            ]
            ++ optionals accountPassword [
              "--account-password-file"
              "/run/account-password"
            ]
          );
          BindReadOnlyPaths =
            runtimePaths
            ++ (
              if managed then
                [ socketDirectory ]
              else
                [
                  "/etc/resolv.conf"
                  "/etc/hosts"
                ]
            )
            ++ optional accountPassword "-${accountPasswordCopy}:/run/account-password";
          BindPaths = [ "${cfg.stateDirectory}:/data" ];
        };
      };
      # The copy of the person's password goes once the accounts are there: after each
      # successful run of shijhon-accounts (not after a failed one, which tries again).
      systemd.services.shijhon-accounts-done = mkIf accountPassword {
        description = "Shijhon's copy of your account's password removed";
        wantedBy = [ "shijhon-accounts.service" ];
        requires = [ "shijhon-accounts.service" ];
        after = [ "shijhon-accounts.service" ];
        serviceConfig = {
          Type = "oneshot";
          ExecStart = "${pkgs.coreutils}/bin/rm -f ${accountPasswordCopy}";
          ProtectSystem = "strict";
          ReadWritePaths = [ "-${dirOf accountPasswordCopy}" ];
          PrivateNetwork = true;
          NoNewPrivileges = true;
          CapabilityBoundingSet = [ "CAP_DAC_OVERRIDE" ];
        };
      };
    })
  ]);
}
