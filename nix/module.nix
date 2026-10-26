{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib)
    mkEnableOption
    mkIf
    mkOption
    types
    ;
  cfg = config.services.shijhon;
  # These paths also appear in systemd's whitespace/colon-separated mount directives.
  absolute = types.strMatching "/[A-Za-z0-9_][A-Za-z0-9_./@+-]*";
  closure = pkgs.closureInfo {
    rootPaths = [
      cfg.package
      pkgs.cacert
      pkgs.tini
    ];
  };
  runtimePaths = lib.filter (s: s != "") (
    lib.splitString "\n" (builtins.readFile "${closure}/store-paths")
  );
  isolation = {
    User = cfg.user;
    Group = cfg.group;
    UMask = "0077";
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
in
{
  options.services.shijhon = {
    enable = mkEnableOption "Shijhon";
    package = mkOption {
      type = types.package;
      description = "Shijhon package, including selected catalog adapters.";
    };
    user = mkOption {
      type = types.str;
      default = "shijhon";
      description = "Existing service user; created only when createUser is true.";
    };
    group = mkOption {
      type = types.str;
      default = "shijhon";
      description = "Existing service group.";
    };
    createUser = mkOption {
      type = types.bool;
      default = true;
      description = "Create a dedicated service user and group. Disable when preserving an existing identity.";
    };
    configDirectory = mkOption {
      type = absolute;
      default = "/etc/shijhon";
      description = "Private configuration directory, bound read-only at /config. Must contain shijhon.toml; keep secrets out of Nix expressions.";
    };
    stateDirectory = mkOption {
      type = absolute;
      default = "/var/lib/shijhon";
      description = "Existing persistent state, bound at /data. Created if absent; existing contents are never recursively chowned.";
    };
    libraryPath = mkOption {
      type = absolute;
      description = "Existing library root, bound read-only at /music. Navidrome must see the same files at /music.";
    };
    placeholderPath = mkOption {
      type = absolute;
      description = "Existing writable placeholder folder, bound over /music/<placeholderFolder>. No library folder or ownership is changed.";
    };
    placeholderFolder = mkOption {
      type = types.strMatching "[A-Za-z0-9_.-]+";
      default = "_shijhon";
      description = "Placeholder directory name inside the library.";
    };
    importsPath = mkOption {
      type = types.nullOr absolute;
      default = null;
      description = "Existing dedicated retained-original folder, writable at /music/<importsFolder>. Navidrome must see the same files. Never place this inside disposable placeholder storage; provision permissions and backups separately.";
    };
    importsFolder = mkOption {
      type = types.strMatching "[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}";
      default = "_imports";
      description = "Retained-original directory name inside the library.";
    };
    usageDirectory = mkOption {
      type = types.nullOr absolute;
      default = null;
      description = "Usage export directory, bound read/write at /usage for export requests.";
    };
    port = mkOption {
      type = types.port;
      default = 8765;
      description = "Loopback listener port.";
    };
    usageExport = {
      enable = mkEnableOption "the isolated, networkless Navidrome usage exporter";
      navidromeDataDirectory = mkOption {
        type = absolute;
        description = "Navidrome data directory, visible read-only only to the exporter (including WAL sidecars).";
      };
      everySeconds = mkOption {
        type = types.ints.positive;
        default = 900;
        description = "Periodic export interval. Request-file exports are also served.";
      };
    };
  };

  config = mkIf cfg.enable {
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
        assertion = !cfg.usageExport.enable || cfg.usageDirectory != null;
        message = "services.shijhon.usageExport requires usageDirectory.";
      }
      {
        assertion = cfg.importsPath == null || (
          cfg.importsFolder != cfg.placeholderFolder
          && cfg.importsPath != cfg.placeholderPath
          && !lib.hasPrefix (cfg.placeholderPath + "/") cfg.importsPath
          && !lib.hasPrefix (cfg.importsPath + "/") cfg.placeholderPath
        );
        message = "services.shijhon imports and disposable placeholders need separate folders.";
      }
    ];
    users.users = mkIf cfg.createUser {
      ${cfg.user} = {
        isSystemUser = true;
        group = cfg.group;
      };
    };
    users.groups = mkIf cfg.createUser { ${cfg.group} = { }; };
    systemd.tmpfiles.rules = [
      "d '${cfg.stateDirectory}' 0700 ${cfg.user} ${cfg.group} - -"
      "d /run/shijhon-root 0755 root root - -"
    ]
    ++ lib.optional cfg.usageExport.enable "d /run/shijhon-usage-root 0755 root root - -";
    systemd.services.shijhon = {
      description = "Shijhon music library proxy";
      wantedBy = [ "multi-user.target" ];
      wants = [
        "network-online.target"
      ]
      ++ lib.optional cfg.usageExport.enable "shijhon-usage-export.service";
      after = [
        "network-online.target"
      ]
      ++ lib.optional cfg.usageExport.enable "shijhon-usage-export.service";
      unitConfig.RequiresMountsFor = [
        cfg.stateDirectory
        cfg.configDirectory
        cfg.libraryPath
        cfg.placeholderPath
      ]
      ++ lib.optional (cfg.importsPath != null) cfg.importsPath
      ++ lib.optional (cfg.usageDirectory != null) cfg.usageDirectory;
      restartTriggers = [
        cfg.package
        closure
      ];
      environment = {
        PYTHONDONTWRITEBYTECODE = "1";
        PYTHONUNBUFFERED = "1";
        SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
        SHIJHON_STATE_DIR = "/data";
        SHIJHON_SERVER__HOST = "127.0.0.1";
        SHIJHON_SERVER__PORT = toString cfg.port;
        SHIJHON_SERVER__RESTART_BY_SUPERVISOR = "true";
        SHIJHON_NAVIDROME__LIBRARY_PATH = "/music";
        SHIJHON_PLACEHOLDERS__FOLDER = cfg.placeholderFolder;
      }
      // lib.optionalAttrs (cfg.importsPath != null) {
        SHIJHON_MUSIC_IMPORTS__ENABLED = "true";
        SHIJHON_MUSIC_IMPORTS__FOLDER = cfg.importsFolder;
      }
      // lib.optionalAttrs (cfg.usageDirectory != null) {
        SHIJHON_NAVIDROME__USAGE_EXPORT_PATH = "/usage/usage.sqlite3";
      };
      serviceConfig = isolation // {
        RootDirectory = "/run/shijhon-root";
        NoExecPaths =
          isolation.NoExecPaths
          ++ [
            "+/config"
            "+/data"
            "+/music"
            "+/music/${cfg.placeholderFolder}"
          ]
          ++ lib.optional (cfg.importsPath != null) "+/music/${cfg.importsFolder}"
          ++ lib.optional (cfg.usageDirectory != null) "+/usage";
        WorkingDirectory = "/data";
        ExecStart = lib.escapeShellArgs [
          "${pkgs.tini}/bin/tini"
          "--"
          "${cfg.package}/bin/shijhon"
          "--config"
          "/config/shijhon.toml"
          "serve"
        ];
        BindReadOnlyPaths = runtimePaths ++ [
          "${cfg.configDirectory}:/config"
          "${cfg.libraryPath}:/music"
          "/etc/resolv.conf"
          "/etc/hosts"
        ];
        BindPaths = [
          "${cfg.stateDirectory}:/data"
          "${cfg.placeholderPath}:/music/${cfg.placeholderFolder}"
        ]
        ++ lib.optional (cfg.importsPath != null) "${cfg.importsPath}:/music/${cfg.importsFolder}"
        ++ lib.optional (cfg.usageDirectory != null) "${cfg.usageDirectory}:/usage";
      };
    };
    systemd.services.shijhon-usage-export = mkIf cfg.usageExport.enable {
      description = "Shijhon Navidrome usage export";
      wantedBy = [ "multi-user.target" ];
      unitConfig.RequiresMountsFor = [
        cfg.usageExport.navidromeDataDirectory
        cfg.usageDirectory
      ];
      serviceConfig = isolation // {
        RootDirectory = "/run/shijhon-usage-root";
        NoExecPaths = isolation.NoExecPaths ++ [
          "+/navidrome-data"
          "+/usage"
        ];
        PrivateNetwork = true;
        ExecStart = lib.escapeShellArgs [
          "${pkgs.tini}/bin/tini"
          "--"
          "${cfg.package}/bin/shijhon"
          "usage-export"
          "--navidrome-db"
          "/navidrome-data/navidrome.db"
          "--out"
          "/usage/usage.sqlite3"
          "--every"
          (toString cfg.usageExport.everySeconds)
        ];
        BindReadOnlyPaths = runtimePaths ++ [ "${cfg.usageExport.navidromeDataDirectory}:/navidrome-data" ];
        BindPaths = [ "${cfg.usageDirectory}:/usage" ];
      };
    };
  };
}
