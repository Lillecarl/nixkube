# SPDX-License-Identifier: Apache-2.0

{
  pkgs,
  lib,
  config,
  ...
}:
{
  options.logger.files = lib.mkOption {
    type = lib.types.listOf lib.types.str;
    default = [ ];
  };
  config = {
    services.logger =
      let
        fileStr = lib.pipe config.logger.files [
          (lib.map (file: "/var/log/${file}"))
          (lib.join " ")
        ];
      in
      {
        command = "${lib.getExe' pkgs.coreutils "tail"} --retry --follow=name ${fileStr}";
        options = [ "shares-console" ];
      };
  };
}
