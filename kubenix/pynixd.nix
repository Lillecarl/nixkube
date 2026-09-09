# SPDX-License-Identifier: MIT

{
  config,
  lib,
  csiPkgs,
  curPkgs,
  ...
}:
let
  cfg = config.nixkube;
  nsRes = config.kubernetes.resources.${cfg.namespace};

  # Shared across pynixd central and builders
  image = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1";
  storeVolumeAttributes = lib.mapAttrs (_: pkgs: pkgs.nixkube-pynixd-env) csiPkgs;

  pynixdLabels = cfg.labels // {
    "app.kubernetes.io/component" = "pynixd";
  };
  pynixdMatchLabels = cfg.matchLabels // {
    "app.kubernetes.io/component" = "pynixd";
  };
  builderLabels = cfg.labels // {
    "app.kubernetes.io/component" = "builder";
  };

  # Only enabled systems
  enabledSystems = lib.filterAttrs (_: v: v) cfg.systems;

  pynixdSettings = lib.mkOption {
    description = ''
      Pynixd configuration as a JSON object. Merged into the PYNIXD_CONFIG
      config file mounted in the pynixd pod. Corresponds to the PynixdSettings
      pydantic model (see pynixd.config).

      Common keys include stores (dict of StoreSpec keyed by store ID),
      ranking weights, GC intervals, etc. When stores include SSH stores,
      their client keys are auto-discovered from HOME/.ssh/ if client_keys
      is omitted.
    '';
    type = jsonFormat.type;
    default = { };
    example = lib.literalExpression ''
      {
        stores = {
          builder1 = {
            type = "ssh-subprocess";
            host = "builder.example.com";
            port = 22;
            username = "nix";
            systems = [ "x86_64-linux" ];
          };
        };
      }
    '';
  };

  jsonFormat = curPkgs.formats.json { };
in
{
  options.nixkube.pynixd = {
    enable =
      (lib.mkEnableOption "pynixd StatefulSet (shared Nix binary cache and build distributor)")
      // {
        default = true;
      };
    settings = pynixdSettings;

    controller = {
      settings = pynixdSettings;
      nixConfig = lib.mkOption {
        description = "nix.conf for pynixd pod";
        type = (import ./nixOptions.nix) {
          pkgs = curPkgs;
          nix = config.nixkube.nix.package;
        };
      };
    };
    authorizedKeys = lib.mkOption {
      description = "SSH public keys that can connect to cache. Used by nodes to push built store paths to the cache.";
      type = lib.types.listOf (lib.types.either lib.types.str lib.types.path);
      apply = lib.map (v: lib.trim (if lib.typeOf v == "path" then builtins.readFile v else v));
      default = [ ];
      example = lib.literalExpression ''
        [
          "ssh-ed25519 AAAA... user@host"
          ./keys/deploy.pub
        ]
      '';
    };
    storageClassName = lib.mkOption {
      description = "StorageClass for the pynixd PVC. null uses the cluster's default StorageClass.";
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "fast-ssd";
    };
    loadBalancerPort = lib.mkOption {
      description = ''
        External SSH port for the pynixd LoadBalancer Service.
        Set to null to disable the LoadBalancer (cluster-internal access only).
      '';
      type = lib.types.nullOr lib.types.int;
      default = 2222;
    };
    builder = {
      settings = pynixdSettings;
      nixConfig = lib.mkOption {
        description = "nix.conf for builder pods";
        type = (import ./nixOptions.nix) {
          pkgs = curPkgs;
          nix = config.nixkube.nix.package;
        };
      };
    };
    extraVolumes = lib.mkOption {
      description = ''
        Extra Kubernetes volumes keyed by name. Merged into the
        StatefulSet pod spec volumes. Useful for mounting Secrets
        containing SSH client keys for external stores.
      '';
      type = lib.types.attrsOf jsonFormat.type;
      default = { };
      example = lib.literalExpression ''
        {
          my-builder-key.secret.secretName = "my-builder-key";
        }
      '';
    };
    extraVolumeMounts = lib.mkOption {
      description = ''
        Extra volume mounts keyed by name. Merged into the pynixd
        container volumeMounts. Mount external SSH client keys into
        HOME/.ssh/ for asyncssh auto-discovery.
      '';
      type = lib.types.attrsOf jsonFormat.type;
      default = { };
      example = lib.literalExpression ''
        {
          my-builder-key.mountPath = "/nix/var/nix-csi/root/.ssh/id_ed25519";
        }
      '';
    };
  };
  config = lib.mkIf (cfg.enable && cfg.pynixd.enable) {
    # shared settings -> controller settings
    #
    # The defaults here are mkOptionDefault, and the shared settings pass
    # through with whatever priority the consumer gave them.
    #
    # Both sides used to be wrapped in mkDefault, which made
    # `nixkube.pynixd.settings` inert for exactly the keys that have a default:
    # two mkDefault definitions of builder-max conflict rather than one winning,
    # and mkForce did not help either, because mapAttrsRecursive re-wrapped it.
    # So an operator could not cap or disable builders at all, and the option
    # that looks like the way to do it failed with a priority conflict.
    nixkube.pynixd.controller.settings = lib.mkMerge [
      (lib.mapAttrsRecursive (n: v: lib.mkOptionDefault v) {
        builder-max = 3;
        builder-min = 1;
        idle-timeout = 300;
      })
      config.nixkube.pynixd.settings
    ];
    # shared settings -> builder settings
    nixkube.pynixd.builder.settings = lib.mkMerge [
      (lib.mapAttrsRecursive (n: v: lib.mkDefault v) {
        # builder-specific JSON defaults go here (e.g., schedule-mode)
        #
        # Empty on purpose. builder-config then renders `config.json = {}`,
        # which reads like a bug and is not one: it means nothing is
        # configured, and PynixdSettings() supplies its own defaults. It has
        # been reported as a fault once already.
      })
      config.nixkube.pynixd.settings
    ];

    nixkube.pynixd.builder.nixConfig.settings = {
      max-jobs = lib.mkDefault 5;
      warn-dirty = lib.mkDefault false;
    };

    kubernetes.resources.${cfg.namespace} = {
      StatefulSet.pynixd = {
        metadata.labels = pynixdLabels;
        metadata.annotations."nixkube/discard" = "true";
        spec = {
          serviceName = "pynixd";
          replicas = 1;
          selector.matchLabels = pynixdLabels;
          template = {
            metadata.labels = pynixdLabels;
            metadata.annotations = {
              "kubectl.kubernetes.io/default-container" = "pynixd";
              configHash = lib.hashAttrs (
                { }
                // nsRes.ConfigMap.pynixd or { }
                // nsRes.ConfigMap.ssh-config or { }
                // nsRes.ConfigMap.pynixd-config or { }
              );
            };
            spec = {
              serviceAccountName = "nixkube";
              priorityClassName = "system-cluster-critical";

              containers = lib.mkNamedList {
                pynixd = {
                  command = [
                    "tini"
                    "--"
                    "pynixd-nixkube-central"
                  ];
                  inherit image;
                  env = lib.mkNamedList {
                    PYNIXD_ENABLED.value = lib.boolToString cfg.pynixd.enable;
                    PYNIXD_SSH_HOST.value = "";
                    PYNIXD_SSH_PORT.value = "22";
                    PYNIXD_HTTP_PORT.value = "8080";
                    PYNIXD_SSH_HOST_KEY.value = "/etc/ssh-key/id_ed25519";
                    HOME.value = "/data/var/nix-csi/root";
                    PYNIXD_KUBE_NAMESPACE.valueFrom.fieldRef.fieldPath = "metadata.namespace";
                    PYNIXD_BUILDER_MAX.value = "3";
                    PYNIXD_BUILDER_MIN.value = "1";
                    PYNIXD_IDLE_TIMEOUT.value = "300";
                    PYNIXD_SCHEDULE_MODE.value = "scheduler";
                    PYNIXD_SYSTEMS.value = lib.concatStringsSep "," (builtins.attrNames enabledSystems);
                    PYNIXD_CONFIG.value = "/etc/pynixd-config/config.json";
                  };
                  ports = lib.mkNamedList {
                    ssh.containerPort = 22;
                  };
                  readinessProbe.tcpSocket.port = "ssh";
                  livenessProbe.tcpSocket.port = "ssh";
                  volumeMounts = lib.mkNamedList (
                    {
                      nix-config.mountPath = "/etc/nix";
                      nix-key.mountPath = "/etc/nix-key";
                      nix-store.mountPath = "/data";
                      init-store = {
                        mountPath = "/nix";
                        subPath = "nix";
                        readOnly = true;
                      };

                      ssh-config.mountPath = "/etc/ssh";
                      ssh-dynauth.mountPath = "/etc/ssh-dynauth";
                      ssh-key.mountPath = "/etc/ssh-key";
                      pynixd-config.mountPath = "/etc/pynixd-config";
                    }
                    // cfg.pynixd.extraVolumeMounts
                  );
                  resources = {
                    requests = {
                      memory = "64Mi";
                      cpu = "100m";
                    };
                  };
                };
              };
              volumes = lib.mkNamedList (
                {
                  nix-config.configMap.name = "pynixd";
                  nix-key.secret.secretName = "nix-key";
                  init-store.csi = {
                    driver = "nixkube";
                    readOnly = true;
                    volumeAttributes = storeVolumeAttributes;
                  };

                  ssh-config.configMap = {
                    name = "ssh-config";
                    defaultMode = 292; # 444
                  };
                  ssh-dynauth.configMap = {
                    name = "ssh-dynauth";
                    defaultMode = 292; # 444
                  };
                  ssh-key.secret = {
                    secretName = "ssh-key";
                    defaultMode = 256; # 400
                  };
                  pynixd-config.configMap = {
                    name = "pynixd-config";
                    defaultMode = 292; # 444
                  };
                }
                // cfg.pynixd.extraVolumes
              );
            };
          };
          volumeClaimTemplates = lib.mkNumberedList {
            "1" = {
              metadata.name = "nix-store";
              spec = {
                accessModes = [ "ReadWriteOnce" ];
                resources.requests.storage = "10Gi";
                inherit (cfg.pynixd) storageClassName;
              };
            };
          };
        };
      };

      Service.pynixd = {
        metadata.labels = pynixdLabels;
        spec = {
          selector = pynixdMatchLabels;
          ports = lib.mkNamedList {
            ssh = {
              port = 22;
              targetPort = "ssh";
            };
          };
          type = "ClusterIP";
        };
      };
      Service.pynixd-lb = lib.mkIf (cfg.pynixd.loadBalancerPort != null) {
        metadata.labels = pynixdLabels;
        spec = {
          selector = pynixdMatchLabels;
          ports = lib.mkNamedList {
            ssh = {
              port = cfg.pynixd.loadBalancerPort;
              targetPort = "ssh";
            };
          };
          type = "LoadBalancer";
        };
      };

      ConfigMap.builder = {
        metadata.labels = builderLabels;
        data = {
          "nix.conf" = builtins.readFile cfg.pynixd.builder.nixConfig.nixConf;
        };
      };
      ConfigMap.pynixd-config = {
        metadata.labels = pynixdLabels;
        data = {
          "config.json" = builtins.toJSON cfg.pynixd.controller.settings;
        };
      };
      ConfigMap.builder-config = {
        metadata.labels = builderLabels;
        data = {
          "config.json" = builtins.toJSON cfg.pynixd.builder.settings;
        };
      };

      PodTemplate.nixkube-builder = {
        metadata.labels = builderLabels;
        template = {
          metadata.labels = builderLabels;
          spec = {
            serviceAccountName = "nixkube";
            restartPolicy = "Never";
            affinity = {
              podAntiAffinity = {
                preferredDuringSchedulingIgnoredDuringExecution = [
                  {
                    weight = 100;
                    podAffinityTerm = {
                      topologyKey = "kubernetes.io/hostname";
                      labelSelector.matchLabels = builderLabels;
                    };
                  }
                ];
              };
            };
            containers = lib.mkNamedList {
              pynixd = {
                command = [
                  "/nix/var/result/bin/tini"
                  "--"
                  "/nix/var/result/bin/pynixd-nixkube-builder"
                ];
                inherit image;
                env = lib.mkNamedList {
                  PYNIXD_SSH_HOST.value = "";
                  PYNIXD_SSH_PORT.value = "22";
                  PYNIXD_HTTP_PORT.value = "8080";
                  PYNIXD_IDLE_TIMEOUT.value = "300";
                  HOME.value = "/nix/var/nix-csi/root";
                  PYNIXD_CONFIG.value = "/nix/etc/builder-config/config.json";
                };
                ports = lib.mkNamedList {
                  ssh.containerPort = 22;
                };
                readinessProbe.tcpSocket.port = "ssh";
                livenessProbe.tcpSocket.port = "ssh";
                volumeMounts = lib.mkNamedList {
                  nix-config.mountPath = "/etc/nix";
                  nix-store = {
                    mountPath = "/nix";
                    subPath = "nix";
                  };
                  builder-config.mountPath = "/nix/etc/builder-config";
                };
                resources = {
                  requests = {
                    memory = "256Mi";
                    cpu = "250m";
                  };
                };
              };
            };
            volumes = lib.mkNamedList {
              nix-config.configMap.name = "builder";
              nix-store.csi = {
                driver = "nixkube";
                readOnly = false;
                volumeAttributes = storeVolumeAttributes;
              };
              builder-config.configMap.name = "builder-config";
            };
          };
        };
      };
    };
  };
}
