# SPDX-License-Identifier: MIT

# One pod that uses both of nixkube's mount paths, created by the test rather
# than applied with the manifest.
#
# Created, and not applied, for two reasons.
#
# The chaos scenarios all ask the same question after they break something:
# can a pod still start? A pod that was already running does not answer it --
# it has its mounts already. So each scenario creates one of these, reads it,
# and deletes it. `generateName` is what lets that happen again and again,
# and an object in a manifest cannot have one: `kubectl apply` needs a name.
#
# The other reason is a race, and it is the reason the NRI containers moved
# here out of ./workloads.nix. A pod with a CSI volume waits for the driver
# by construction -- kubelet cannot publish the volume until the driver
# registers. A pod that only wants NRI waits for nothing. Applied with the
# manifest, it starts while the DaemonSet is still running its init
# container, the plugin never sees it, and it gets no /nix.
#
# In a real cluster that pod then fails, because there is no /nix to run
# from. Here it does not: user-mode-nixos binds /nix/store into every
# container, so it runs and proves nothing. Creating the pod after the
# driver is Ready is what makes the NRI answer mean something.
{
  pkgs,
  lib,
}:
let
  system = pkgs.stdenv.hostPlatform.system;
  scratch = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1";

  # Reached through the volume, and nowhere else -- /nix/store is bound into
  # every container in this guest, so a command under /nix/store would run
  # with the driver switched off. See ./workloads.nix.
  mountPath = "/mnt/csi";

  probe =
    {
      name,
      annotations ? { },
    }:
    pkgs.writeText "nixkube-probe-${name}.json" (
      builtins.toJSON {
        apiVersion = "batch/v1";
        kind = "Job";
        metadata = {
          generateName = "probe-${name}-";
          namespace = "nixkube";
          labels."app.kubernetes.io/component" = "uml-probe";
        };
        spec = {
          backoffLimit = 0;
          template = {
            metadata = lib.optionalAttrs (annotations != { }) { inherit annotations; };
            spec = {
              restartPolicy = "Never";
              containers = [
                # The CSI path: a volume the pod asked for, at the path it
                # asked for it. Only the driver can put a file there.
                {
                  name = "csi";
                  image = scratch;
                  imagePullPolicy = "Never";
                  command = [ "${mountPath}${lib.getExe pkgs.hello}" ];
                  volumeMounts = [
                    {
                      name = "store";
                      mountPath = mountPath;
                    }
                  ];
                }
                # The NRI path: no volume, a store path in the command, and
                # its own mount table as the answer. A mount point of
                # exactly `/nix` is the plugin's work and nobody else's.
                {
                  name = "nri";
                  image = scratch;
                  imagePullPolicy = "Never";
                  command = [
                    "${pkgs.busybox}/bin/cat"
                    "/proc/self/mountinfo"
                  ];
                }
              ];
              volumes = [
                {
                  name = "store";
                  csi = {
                    driver = "nixkube";
                    readOnly = true;
                    volumeAttributes.${system} = "${pkgs.hello}";
                  };
                }
              ];
            };
          };
        };
      }
    );
in
{
  ro = probe { name = "ro"; };

  # The same, read-write. The plugin builds this one as an overlay rather
  # than a clone of the node's /nix, and the mount options say which
  # happened -- the filesystem type cannot, because the guest's own /nix is
  # an overlay too.
  rw = probe {
    name = "rw";
    annotations."nixkube/pod-rw" = "true";
  };
}
