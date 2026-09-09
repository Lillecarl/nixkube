# SPDX-License-Identifier: MIT

{
  config,
  lib,
  ...
}:
let
  cfg = config.nixkube;

in
{
  config = lib.mkIf cfg.enable {
    nixkube = {
      nixConfig.settings = {
        allowed-users = [ "*" ];
        trusted-public-keys = [
          "nix-csi.cachix.org-1:i4w33gR4efO67jpz8U7g/MdvRQ6mQ3LEF9fB8tES60g="
        ];
        substituters = [
          "https://nix-csi.cachix.org"
        ];
        experimental-features = [
          "nix-command"
          "flakes"
          "read-only-local-store"
          "ca-derivations"
          "dynamic-derivations"
          "recursive-nix"
        ];
        builders-use-substitutes = true;
        narinfo-cache-negative-ttl = 0;
        narinfo-cache-positive-ttl = 0;
        warn-dirty = false;
        store = "daemon";
        system-features = [
          "nixos-test"
          "benchmark"
          "big-parallel"
        ];
      };
      # One settings set, three roles, and `store = "daemon"` above is true of
      # only two of them: the builder Pod runs no system nix-daemon.
      #
      # Measured, and it does no harm there. pynixd spawns the builder's daemon
      # itself and passes `--store /` on its command line, which overrides the
      # `store` of nix.conf. Nothing else in that Pod runs the nix CLI.
      #
      # So this is a smell rather than a fault, and it was not the cause of
      # issue #19 although it looks like it. Splitting the set per role is
      # still right -- a role should not be able to inherit a setting it cannot
      # satisfy -- but it needs a decision about what the builder's `store`
      # becomes, and unsetting it changes the CLI default to `auto`.
      node.nixConfig.settings = cfg.nixConfig.settings;
      pynixd.builder.nixConfig.settings = cfg.nixConfig.settings;
      pynixd.controller.nixConfig.settings = cfg.nixConfig.settings;
    };
    kubernetes.resources.${cfg.namespace} = {
      ConfigMap.nix-node = {
        metadata.labels = cfg.labels;
        data = {
          "nix.conf" = builtins.readFile (cfg.node.nixConfig.nixConf);
          "logging.json" = builtins.toJSON cfg.loggingConfig;
        };
      };
      ConfigMap.pynixd = {
        metadata.labels = cfg.labels;
        data = {
          "nix.conf" = builtins.readFile (cfg.pynixd.controller.nixConfig.nixConf);
          "logging.json" = builtins.toJSON cfg.loggingConfig;
        };
      };
    };
  };
}
