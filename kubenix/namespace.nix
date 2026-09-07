# SPDX-License-Identifier: MIT

{
  config,
  lib,
  ...
}:
let
  cfg = config.nixkube;
  namespace = cfg.namespace;
in
{
  config = lib.mkIf cfg.enable {
    kubernetes.resources.none.Namespace.${namespace} = {
      metadata.labels = cfg.labels;
    };

    /*
      The prune scope for a nixkube deploy.

      `ekn.discriminator` has no default: an apply deletes objects carrying
      this label that the apply did not produce, so two projects sharing a
      value on one cluster delete each other's work. `mkDefault`, because a
      deployment that installs nixkube beside its own objects wants one
      scope for all of them and says so itself.
    */
    ekn.discriminator = lib.mkDefault namespace;
  };
}
