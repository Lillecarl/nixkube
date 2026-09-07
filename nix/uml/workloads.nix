# SPDX-License-Identifier: MIT

# What a workload asks nixkube for, and how the test knows it got it.
#
# kubenix/ci carries workloads already, and most of them cannot run here:
# `flake-hello` fetches from GitHub, `expr-hello` builds from nixpkgs, and a
# build sandbox has no network. These are the offline half, written out
# rather than filtered, so each one says what it proves.
#
# One thing has to be said about proof. user-mode-nixos bind-mounts the
# guest's /nix/store into *every* container, because kubeadm's own images are
# symlink farms into the store. So a container that runs a store path proves
# nothing on its own -- it would run with the driver switched off. Every job
# here therefore reaches its binary through the path the pod asked the driver
# for, and nowhere else.
{
  config,
  curPkgs,
  lib,
  ...
}:
let
  cfg = config.nixkube;

  # Not /nix. The store is already at /nix in every container here, so a
  # volume mounted there would prove nothing.
  mountPath = "/mnt/csi";

  system = curPkgs.stdenv.hostPlatform.system;

  labels = cfg.labels // {
    "app.kubernetes.io/component" = "uml-test";
  };

  # A CSI ephemeral volume holding one store path, and nothing else.
  storeVolume = lib.mkNamedList {
    store.csi = {
      driver = "nixkube";
      readOnly = true;
      volumeAttributes.${system} = "${curPkgs.hello}";
    };
  };

  # `hello`, reached through the volume: /mnt/csi/nix/store/...-hello/bin/hello.
  # Only the driver can put a file at that path.
  helloThroughVolume = lib.mkNamedList {
    hello = {
      image = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1";
      command = [ "${mountPath}${lib.getExe curPkgs.hello}" ];
      volumeMounts = lib.mkNamedList {
        store.mountPath = mountPath;
      };
    };
  };

  job = spec: {
    metadata.labels = labels;
    spec = {
      # No retries. A workload that needs a second attempt here has found a
      # bug, and six of them only hide it behind five more minutes.
      backoffLimit = 0;
      template.spec = {
        restartPolicy = "Never";
      } // spec;
    };
  };
in
{
  config.kubernetes.resources.${cfg.namespace} = {
    # One pod, one store path, mounted where the pod asked.
    Job.csi-path = job {
      containers = helloThroughVolume;
      volumes = storeVolume;
    };

    # The same store path, asked for again by a second pod at the same time.
    # A driver that mounts per volume and unmounts per volume passes this; one
    # that shares a mount and unmounts it on the first delete does not.
    Job.csi-shared = job {
      containers = helloThroughVolume;
      volumes = storeVolume;
    };
  };
}
