{
  pyproject-nix,
  uv2nix,
  pyproject-build-systems,
}:
{
  pkgs,
  python ? pkgs.python312,
  extraOverlays ? [ ],
  extraPackages ? [ ],
}:
let
  inherit (pkgs) lib;
  workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ../.; };
  pythonSet = (pkgs.callPackage pyproject-nix.build.packages { inherit python; }).overrideScope (
    lib.composeManyExtensions (
      [
        pyproject-build-systems.overlays.wheel
        (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
        (final: prev: {
          shijhon = prev.shijhon.overrideAttrs (old: {
            # Only what the wheel needs: no local configuration or tests.
            src = lib.fileset.toSource {
              root = ../.;
              fileset = lib.fileset.unions [
                ../pyproject.toml
                ../README.md
                ../LICENSE
                ../NOTICE
                (lib.fileset.fileFilter (
                  file: !(lib.hasSuffix ".pyc" file.name) && file.name != ".DS_Store"
                ) ../src)
              ];
            };
          });
        })
      ]
      ++ extraOverlays
    )
  );
  environment =
    (pythonSet.mkVirtualEnv "shijhon-env" ({ shijhon = [ ]; } // lib.genAttrs extraPackages (_: [ ])))
    .overrideAttrs
      (old: {
        postInstall = (old.postInstall or "") + ''
          UV_CACHE_DIR="$TMPDIR/uv-cache" ${pkgs.uv}/bin/uv pip check --python "$out/bin/python"
        '';
      });
  application = (pkgs.callPackages pyproject-nix.build.util { }).mkApplication {
    venv = environment;
    package = pythonSet.shijhon;
  };
in
assert lib.assertMsg (lib.versionAtLeast python.version "3.12") "Shijhon requires Python >= 3.12";
application.overrideAttrs (old: {
  nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ pkgs.makeWrapper ];
  postInstall = (old.postInstall or "") + ''
    wrapProgram "$out/bin/shijhon" --prefix PATH : ${lib.makeBinPath [ pkgs.ffmpeg ]}
  '';
  passthru = (old.passthru or { }) // {
    inherit pythonSet;
    pythonEnvironment = environment;
  };
  meta = {
    description = "Blend your music library with a catalog for Subsonic clients";
    homepage = "https://github.com/Jasshl/shijhon";
    license = lib.licenses.agpl3Plus;
    platforms = lib.platforms.linux;
    mainProgram = "shijhon";
  };
})
