# SPDX-License-Identifier: Apache-2.0

{
  config,
  lib,
  ...
}:
let
  cfg = config.nixkube;
in
{
  config = lib.mkIf cfg.undeploy {
    assertions = [
      {
        assertion = !(cfg.enable && cfg.undeploy);
        message = "nixkube.undeploy cannot be true when nixkube.enable is also true.";
      }
      {
        assertion = !(cfg.undeploy && lib.hasPrefix "/nix" cfg.hostMountPath);
        message = "nixkube.undeploy will not undeploy /nix based mount locations like ${cfg.hostMountPath}";
      }
    ];

    kubernetes.resources.${cfg.namespace}.DaemonSet.nixkube-cleanup =
      let
        labels = cfg.labels // {
          "app.kubernetes.io/component" = "cleanup";
        };
        matchLabels = cfg.matchLabels // {
          "app.kubernetes.io/component" = "cleanup";
        };
      in
      {
        metadata.labels = labels;
        spec.selector.matchLabels = matchLabels;
        spec.template = {
          metadata.labels = labels;
          spec = {
            containers = lib.mkNamedList {
              cleanup = {
                name = "cleanup";
                image = "busybox:latest";
                command = [
                  "find"
                  "/nix"
                  "-mindepth"
                  "1"
                  "-delete"
                ];

                volumeMounts = lib.mkNamedList {
                  nix-store.mountPath = "/nix";
                };
                securityContext.privileged = true;
              };
            };

            volumes = lib.mkNamedList {
              nix-store.hostPath.path = cfg.hostMountPath;
            };
          };
        };
      };
  };
}
