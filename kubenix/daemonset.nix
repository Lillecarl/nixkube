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
in
{
  options.nixkube.node = {
    enable = (lib.mkEnableOption "node DaemonSet (CSI driver and NRI plugin)") // {
      default = true;
    };
    compat = (lib.mkEnableOption "nix.csi.store CSI driver (for backwards compatibility)") // {
      default = true;
      apply =
        value:
        if value then
          lib.warn "nixkube.node.compat: CSI compatibility driver (nix.csi.store) is enabled. This is deprecated and will be removed in a future release. Please migrate to the nixkube driver name." value
        else
          value;
    };
    nixConfig = lib.mkOption {
      description = "nix.conf for CSI/mounter/DaemonSet pods";
      type = (import ./nixOptions.nix) {
        pkgs = curPkgs;
        nix = config.nixkube.nix.package;
      };
    };
  };
  config =
    let
      labels = cfg.labels // {
        "app.kubernetes.io/component" = "node";
      };
      matchLabels = cfg.matchLabels // {
        "app.kubernetes.io/component" = "node";
      };
    in
    lib.mkIf cfg.enable {
      kubernetes.resources.${cfg.namespace} = {
        DaemonSet.nix-node = {
          metadata.labels = labels;
          metadata.annotations."nixkube/discard" = "true";
          spec = {
            updateStrategy = {
              type = "RollingUpdate";
              rollingUpdate.maxUnavailable = 1;
            };
            selector.matchLabels = matchLabels;
            template = {
              metadata.labels = labels;
              metadata.annotations = {
                "kubectl.kubernetes.io/default-container" = "nix-node";
                configHash = lib.hashAttrs (
                  { } // nsRes.ConfigMap.nix-node or { } // nsRes.configMap.ssh-config or { }
                );
              };
              spec = {
                serviceAccountName = "nixkube";
                priorityClassName = "system-node-critical";
                tolerations = [
                  {
                    key = "node-role.kubernetes.io/control-plane";
                    operator = "Exists";
                    effect = "NoSchedule";
                  }
                ];
                initContainers = lib.mkNumberedList {
                  "1" = {
                    name = "initcopy";
                    image = "ghcr.io/lillecarl/nix-csi/nix:${curPkgs.nix.version}-${cfg.version}";
                    imagePullPolicy = "Always";
                    securityContext.privileged = true; # chroot store
                    env = lib.mkNamedList {
                      NODE_ENV.value = builtins.toJSON (lib.mapAttrs (_: sysPkgs: "${sysPkgs.nixkube-node-env}") csiPkgs);
                    };
                    volumeMounts = lib.mkNamedList {
                      nix-store.mountPath = "/nix-volume";
                      nix-config.mountPath = "/etc/nix";

                      ssh-config.mountPath = "/etc/ssh";
                      ssh-key.mountPath = "/etc/ssh-key";
                      ssh-dynauth.mountPath = "/etc/ssh-dynauth";
                    };
                    resources = {
                      requests = {
                        memory = "128Mi";
                        cpu = "100m";
                      };
                    };
                  };
                };
                containers = lib.mkNamedList {
                  nix-node = {
                    image = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1";
                    command = [
                      "tini"
                      "--"
                      "nixkube"
                    ];
                    securityContext.privileged = true;

                    /*
                      Ask the livenessprobe sidecar whether the driver still
                      answers, and restart this container when it does not.

                      The sidecar was already here, calling Probe over
                      /csi/nixkube/csi.sock and serving the answer on 9808.
                      Nothing read it. So a driver that had become
                      unreachable stayed that way, and the only symptom was
                      pods stuck in ContainerCreating with no event to say
                      why -- kubelet cannot report a driver it cannot talk
                      to.

                      Measured: deleting /var/lib/kubelet/plugins/nixkube/
                      csi.sock left the DaemonSet 5/5 Running with 0
                      restarts, and a pod that wanted a volume waited 298
                      seconds without one thing changing. Unlinking the
                      path does not close the listening socket the driver
                      holds, so nothing inside the driver notices either.

                      failureThreshold x periodSeconds is 50s of real
                      unreachability before a restart, and
                      initialDelaySeconds keeps a slow start from counting.
                    */
                    livenessProbe = {
                      httpGet = {
                        path = "/healthz";
                        port = 9808;
                      };
                      initialDelaySeconds = 30;
                      periodSeconds = 10;
                      timeoutSeconds = 5;
                      failureThreshold = 5;
                    };

                    env = lib.mkNamedList {
                      PYNIXD_ENABLED.value = lib.boolToString cfg.pynixd.enable;
                      ENABLE_COMPAT_DRIVER.value = lib.boolToString cfg.node.compat;
                      NRI_ENABLED.value = "true";
                      HOME.value = "/nix/var/nix-csi/root";
                      HOST_MOUNT_PATH.value = cfg.hostMountPath;
                      KUBE_NAMESPACE.valueFrom.fieldRef.fieldPath = "metadata.namespace";
                      KUBE_NODE_NAME.valueFrom.fieldRef.fieldPath = "spec.nodeName";
                      KUBE_POD_IP.valueFrom.fieldRef.fieldPath = "status.podIP";
                      KUBE_POD_NAME.valueFrom.fieldRef.fieldPath = "metadata.name";
                      KUBE_POD_UID.valueFrom.fieldRef.fieldPath = "metadata.uid";
                      NIX_BUILD_TIMEOUT.value = toString cfg.nodeBuildTimeout;
                      VERIFY_STORE_PATHS.value = lib.boolToString cfg.verifyStorePaths;
                      NIXPKGS_ALLOW_UNFREE.value = "1";
                      USER.value = "root";
                    };
                    volumeMounts = lib.mkNamedList {
                      csi-socket.mountPath = "/csi";
                      nix-config.mountPath = "/etc/nix";
                      nri-socket.mountPath = "/var/run/nri";
                      registration.mountPath = "/registration";
                      host-root = {
                        mountPath = "/host";
                      };
                      kubelet = {
                        mountPath = "/var/lib/kubelet";
                        mountPropagation = "Bidirectional";
                      };
                      /*
                        A plain mount of <hostMountPath>/nix, and not `subPath =
                        "nix"` on the parent volume.

                        The kubelet performs a subPath bind itself, in its own
                        mount namespace. A distribution that runs the kubelet in
                        a container does not have <hostMountPath> in that
                        namespace, so the kubelet binds an empty directory over
                        /nix and every store path is missing. The container then
                        cannot exec out of /nix/var/result/bin, and the error
                        names $PATH rather than the store.

                        Measured on Talos v1.13.9, one pod, one hostPath volume,
                        mounted twice:

                          /vol  (plain)           dev 253:5 xfs   nix/store = 242 paths
                          /nix  (subPath: nix)    dev 0:62 overlay   empty

                        and directly, through /proc:

                          host pid 1  mnt:[4026531832]  .../nix/store -> 242 entries
                          kubelet     mnt:[4026532779]  .../nix/store ->   0 entries

                        A plain hostPath mount is handed to the runtime as a
                        path instead, and resolves against the host tree. That
                        is also why initcopy sees a populated store while
                        nix-node does not: initcopy writes through the plain
                        mount at /nix-volume.

                        kubeadm runs its kubelet in the host mount namespace, so
                        no test with a kubeadm control plane can see this.
                        See issue #16.
                      */
                      nix-root = {
                        mountPath = "/nix";
                        mountPropagation = "Bidirectional";
                      };

                      ssh-config.mountPath = "/etc/ssh";
                      ssh-dynauth.mountPath = "/etc/ssh-dynauth";
                      ssh-key.mountPath = "/etc/ssh-key";
                      nix-key.mountPath = "/etc/nix-key";
                    };
                    resources = {
                      requests = {
                        memory = "128Mi";
                        cpu = "100m";
                      };
                    };
                  };
                  csi-node-driver-registrar-nix-csi = lib.mkIf cfg.node.compat {
                    image = "registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.16.0";
                    args = [
                      "--v=5"
                      "--csi-address=/csi/nix.csi.store/csi.sock"
                      "--kubelet-registration-path=/var/lib/kubelet/plugins/nix.csi.store/csi.sock"
                    ];
                    env = lib.mkNamedList {
                      KUBE_NODE_NAME.valueFrom.fieldRef.fieldPath = "spec.nodeName";
                    };
                    volumeMounts = lib.mkNamedList {
                      csi-socket.mountPath = "/csi";
                      kubelet.mountPath = "/var/lib/kubelet";
                      registration.mountPath = "/registration";
                    };
                    resources = {
                      requests = {
                        memory = "10Mi";
                        cpu = "10m";
                      };
                    };
                  };
                  livenessprobe-nix-csi = lib.mkIf cfg.node.compat {
                    image = "registry.k8s.io/sig-storage/livenessprobe:v2.18.0";
                    args = [
                      "--csi-address=/csi/nix.csi.store/csi.sock"
                      "--health-port=9809"
                    ];
                    volumeMounts = lib.mkNamedList {
                      csi-socket.mountPath = "/csi";
                      registration.mountPath = "/registration";
                    };
                    resources = {
                      requests = {
                        memory = "10Mi";
                        cpu = "10m";
                      };
                    };
                  };
                  csi-node-driver-registrar-nixkube = {
                    image = "registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.16.0";
                    args = [
                      "--v=5"
                      "--csi-address=/csi/nixkube/csi.sock"
                      "--kubelet-registration-path=/var/lib/kubelet/plugins/nixkube/csi.sock"
                    ];
                    env = lib.mkNamedList {
                      KUBE_NODE_NAME.valueFrom.fieldRef.fieldPath = "spec.nodeName";
                    };
                    volumeMounts = lib.mkNamedList {
                      csi-socket.mountPath = "/csi";
                      kubelet.mountPath = "/var/lib/kubelet";
                      registration.mountPath = "/registration";
                    };
                    resources = {
                      requests = {
                        memory = "10Mi";
                        cpu = "10m";
                      };
                    };
                  };
                  livenessprobe-nixkube = {
                    image = "registry.k8s.io/sig-storage/livenessprobe:v2.18.0";
                    args = [
                      "--csi-address=/csi/nixkube/csi.sock"
                      "--health-port=9808"
                    ];
                    # The port nix-node's livenessProbe reads. Named so the
                    # two stay together when somebody moves one.
                    ports = lib.mkNamedList {
                      healthz.containerPort = 9808;
                    };
                    volumeMounts = lib.mkNamedList {
                      csi-socket.mountPath = "/csi";
                      registration.mountPath = "/registration";
                    };
                    resources = {
                      requests = {
                        memory = "10Mi";
                        cpu = "10m";
                      };
                    };
                  };
                };
                volumes = lib.mkNamedList {
                  nix-config.configMap.name = "nix-node";
                  registration.hostPath.path = "/var/lib/kubelet/plugins_registry";
                  nix-store.hostPath = {
                    path = cfg.hostMountPath;
                    type = "DirectoryOrCreate";
                  };
                  # The store root, as its own volume, so nix-node mounts it
                  # without subPath. See the nix-root volumeMount above.
                  #
                  # DirectoryOrCreate is required, not a convenience: the
                  # kubelet checks hostPath type when it sets up pod volumes,
                  # which happens before initcopy runs. `Directory` would fail
                  # on a node that has no store yet.
                  nix-root.hostPath = {
                    path = "${cfg.hostMountPath}/nix";
                    type = "DirectoryOrCreate";
                  };
                  csi-socket.hostPath = {
                    path = "/var/lib/kubelet/plugins/";
                    type = "DirectoryOrCreate";
                  };
                  nri-socket.hostPath = {
                    path = "/var/run/nri";
                    type = "DirectoryOrCreate";
                  };
                  host-root.hostPath = {
                    path = "/";
                    type = "Directory";
                  };
                  kubelet.hostPath = {
                    path = "/var/lib/kubelet";
                    type = "Directory";
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
                  nix-key.secret = {
                    secretName = "nix-key";
                    defaultMode = 256; # 400
                  };
                };
              };
            };
          };
        };
      };
    };
}
