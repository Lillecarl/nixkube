# SPDX-License-Identifier: MIT

# One of this repository's applications, as a release build.
#
# nanopynix' `mkApp` builds the venv and projects it, and wraps exactly one
# program, `$out/bin/${name}`. Neither is enough here: `pynixd-nixkube`
# installs three programs and the manifests exec `-central`, and both
# applications need variables set rather than only a PATH. So the wrapping
# happens here and covers every program the venv installs.
#
# **`shutil.which` is how nixkube finds every tool it runs** -- `wait`,
# `coreutils`, `nix`, `git`, `ssh`, `mount`. That is why they belong on this
# PATH and not in `dependencies`: a name in `dependencies` reaches the runtime
# closure of every consumer of the library as well.
{
  lib,
  symlinkJoin,
  makeWrapper,
  mkApp,
}:

{
  name,
  pythonSet,
  # On the PATH of every program here. Tools the program runs as subprocesses.
  pathInputs ? [ ],
  # Set on every program here. Store paths the program reads at start-up.
  env ? { },
  # Merged over what the venv projection already carries (`venv`, `package`,
  # `version`). For a build artefact a consumer reads off the application.
  passthru ? { },
}:

let
  app = mkApp { inherit name pythonSet; };

  wrapperArgs =
    lib.optionals (pathInputs != [ ]) [
      "--prefix"
      "PATH"
      ":"
      (lib.makeBinPath pathInputs)
    ]
    ++ lib.concatLists (
      lib.mapAttrsToList (variable: value: [
        "--set"
        variable
        "${value}"
      ]) env
    );
in
if wrapperArgs == [ ] then
  app.overrideAttrs (old: {
    passthru = old.passthru // passthru;
  })
else
  symlinkJoin {
    name = "${name}-wrapped";
    paths = [ app ];
    nativeBuildInputs = [ makeWrapper ];
    # `rm` first, because the join already linked each program to its name.
    postBuild = ''
      for program in "$out"/bin/*; do
        base=$(basename "$program")
        rm "$program"
        makeWrapper "${app}/bin/$base" "$program" ${lib.escapeShellArgs wrapperArgs}
      done
    '';
    inherit (app) meta;
    passthru = app.passthru // passthru;
  }
