# SPDX-License-Identifier: MIT

# `/etc/passwd` and `/etc/group` for the pynixd container.
#
# `pynixd_nixkube/setup.py` reads this through `FAKE_NSS` at import time and
# installs it into the container root. The 32 `nixbld` users are what a Nix
# daemon needs to run a build at all; `nix` is who the SSH store connects as,
# and `sshd` is the privilege separation account.
{
  lib,
  dockerTools,
}:

let
  nixbldIndices = lib.range 1 32;
  nixbldPasswdLines = map (
    i:
    "nixbld${toString i}:x:${toString (30000 + i)}:30000:Nix build user ${toString i}:/var/empty:/bin/sh"
  ) nixbldIndices;
in
dockerTools.fakeNss.override {
  extraPasswdLines = [
    "nix:x:1000:1000:Nix worker user:/nix/var/nix-csi/root:/bin/sh"
    "sshd:x:993:992:SSH privilege separation user:/var/empty:/bin/sh"
  ]
  ++ nixbldPasswdLines;
  extraGroupLines = [
    "nix:x:1000:"
    "sshd:x:992:"
    "nixbld:x:30000:${lib.concatMapStringsSep "," (i: "nixbld${toString i}") nixbldIndices}"
  ];
}
