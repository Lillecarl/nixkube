# SPDX-License-Identifier: MIT

{
  lib,
  pkgs,
  # From the module, so the list checked is the list a node carries.
  substituters,
}:
pkgs.runCommand "assert-cached"
  {
    nativeBuildInputs = [ pkgs.makeWrapper ];
    meta.mainProgram = "assert-cached";
  }
  ''
    install -Dm755 ${./assert_cached.py} $out/libexec/assert-cached.py
    makeWrapper ${lib.getExe pkgs.python3} $out/bin/assert-cached \
      --add-flags $out/libexec/assert-cached.py \
      --set-default NIXKUBE_SUBSTITUTERS ${lib.escapeShellArg (lib.concatStringsSep " " substituters)}
  ''
