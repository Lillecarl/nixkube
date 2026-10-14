# SPDX-License-Identifier: Apache-2.0

{
  pkgs,
  lib,
  ...
}:
{
  config = {
    services.nix-daemon = {
      command = "${lib.getExe pkgs.nix} daemon --store local";
      depends-on = [ "setup" ];
      log-type = "file";
      logfile = "/var/log/nix-daemon.log";
    };
  };
}
