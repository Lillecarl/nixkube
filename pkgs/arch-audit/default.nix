# SPDX-License-Identifier: MIT

{ lib, pkgs }:
pkgs.runCommand "arch-audit"
  {
    nativeBuildInputs = [ pkgs.makeWrapper ];
    meta.mainProgram = "arch-audit";
  }
  ''
    install -Dm755 ${./arch_audit.py} $out/libexec/arch-audit.py
    makeWrapper ${lib.getExe pkgs.python3} $out/bin/arch-audit \
      --add-flags $out/libexec/arch-audit.py \
      --prefix PATH : ${lib.makeBinPath [ pkgs.nix ]}
  ''
