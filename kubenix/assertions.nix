# SPDX-License-Identifier: MIT

{ config, lib, ... }:
let
  cfg = config.nixkube;

  /*
    A hostPath volume mounted with subPath is a trap on any distribution that
    runs the kubelet in a container.

    The kubelet performs a subPath bind itself, in its own mount namespace. A
    plain mount is handed to the runtime as a path instead, and resolves
    against the host tree. So the kubelet must be able to see the source, and
    for a hostPath volume outside /var/lib/kubelet it often cannot. It then
    binds an empty directory, with no error anywhere.

    A CSI volume is a different case and is allowed. Its source lives under
    /var/lib/kubelet, which a containerised kubelet must have or it cannot
    work at all.

    Both sides are measured, on Talos v1.13.9. One pod, one volume, mounted
    twice -- once plainly and once with subPath -- for each kind of volume:

      hostPath + subPath   source 0:62    overlay, container rootfs, EMPTY
      CSI      + subPath   source 253:5   xfs /dev/vda5, populated

    That contrast is the whole reason this assertion refuses one and permits
    the other.

    This is issue #16. It cost days to find, because the symptom named $PATH
    and not the mount. No test with a kubeadm control plane can catch it --
    kubeadm runs its kubelet in the host mount namespace. See issue #18.
  */

  # A pod template, wherever this kind keeps one. Returns null for a kind that
  # has none, such as a ConfigMap.
  podSpecOf =
    resource:
    resource.spec.template.spec or resource.spec.jobTemplate.spec.template.spec
      or (if resource.spec or null != null && resource.spec ? containers then resource.spec else null);

  offendersIn =
    kind: name: resource:
    let
      pod = podSpecOf resource;
    in
    if pod == null then
      [ ]
    else
      let
        hostPathVolumes = lib.pipe (pod.volumes or [ ]) [
          (lib.filter (volume: volume ? hostPath))
          (map (volume: volume.name))
        ];
        containers = (pod.containers or [ ]) ++ (pod.initContainers or [ ]);
        offendingMounts =
          container:
          lib.pipe (container.volumeMounts or [ ]) [
            (lib.filter (mount: mount ? subPath && lib.elem mount.name hostPathVolumes))
            (map (
              mount:
              "${kind}/${name}: container ${container.name or "?"} mounts hostPath volume "
              + "${mount.name} at ${mount.mountPath} with subPath ${mount.subPath}"
            ))
          ];
      in
      lib.concatMap offendingMounts containers;

  offenders = lib.pipe config.kubernetes.resources [
    (lib.mapAttrsToList (
      _namespace: kinds:
      lib.mapAttrsToList (
        kind: named: lib.mapAttrsToList (name: resource: offendersIn kind name resource) named
      ) kinds
    ))
    lib.flatten
  ];
in
{
  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = offenders == [ ];
        message =
          "A hostPath volume is mounted with subPath. The kubelet resolves a "
          + "subPath bind in its own mount namespace, so this silently mounts an "
          + "empty directory wherever the kubelet runs in a container, such as on "
          + "Talos. Give the subdirectory its own hostPath volume and mount it "
          + "plainly. See issue #16.\n  "
          + lib.concatStringsSep "\n  " offenders;
      }
    ];
  };
}
