# SPDX-License-Identifier: Apache-2.0

{
  pkgs,
  ...
}:
{
  config = {
    logger.files = [
      "setup.log"
    ];
    services.setup = {
      type = "scripted";
      log-type = "file";
      logfile = "/var/log/setup.log";
      options = [ "starts-rwfs" ];
      command = pkgs.writeShellApplication {
        name = "setup";
        runtimeInputs = [
          pkgs.rsync
          pkgs.coreutils
          pkgs.nix
        ];
        text = # bash
          ''
            set -euo pipefail
            set -x
            # Copy in "well-known paths" into container root
            rsync --archive ${pkgs.dockerTools.binSh}/ /
            rsync --archive ${pkgs.dockerTools.caCertificates}/ /
            rsync --archive ${pkgs.dockerTools.usrBinEnv}/ /
          '';
      };
    };
  };
}
