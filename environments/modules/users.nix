# SPDX-License-Identifier: Apache-2.0

{
  pkgs,
  lib,
  ...
}:
{
  imports = [
    {
      # Add nixbld users
      users.users = lib.pipe (lib.range 1 32) [
        (map (i: {
          name = "nixbld${toString i}";
          value = {
            uid = 30000 + i;
            gid = 30000;
          };
        }))
        lib.listToAttrs
      ];
    }
  ];
  config = {
    users = {
      enable = true;
      users.root = {
        shell = pkgs.runtimeShell;
        homeDir = "/nix/var/nix-csi/root";
      };
      users.nix = {
        uid = 1000;
        gid = 1000;
        comment = "Nix worker user";
      };
      groups.nix.gid = 1000;
      groups.nixbld = {
        gid = 30000;
        users = lib.map (i: "nixbld${toString i}") (lib.range 1 32);
      };
      users = {
        sshd = {
          uid = 993;
          gid = 992;
          comment = "SSH privilege separation user";
        };
      };
      groups.sshd.gid = 992;
    };
  };
}
