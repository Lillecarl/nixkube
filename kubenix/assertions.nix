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

  # A list field of a rendered resource. `x or [ ]` is not enough: easykubenix
  # renders through typed submodules whose optional fields default to null, so
  # the attribute is present and null rather than missing. Measured on a
  # consumer tree -- Deployment/hubble-ui has a container with a null
  # volumeMounts, and `or [ ]` let the null through to lib.filter.
  listOf =
    attrs: field:
    let
      value = attrs.${field} or null;
    in
    if value == null then [ ] else value;

  # Present-and-null is not "has a subPath" either. Nothing hit this yet, but
  # `mount ? subPath` would fire on it and print "with subPath null".
  hasSubPath = mount: mount.subPath or null != null;

  offendersIn =
    kind: name: resource:
    let
      pod = podSpecOf resource;
    in
    if pod == null then
      [ ]
    else
      let
        hostPathVolumes = lib.pipe (listOf pod "volumes") [
          (lib.filter (volume: volume ? hostPath))
          (map (volume: volume.name))
        ];
        containers = (listOf pod "containers") ++ (listOf pod "initContainers");
        offendingMounts =
          container:
          lib.pipe (listOf container "volumeMounts") [
            (lib.filter (mount: hasSubPath mount && lib.elem mount.name hostPathVolumes))
            (map (
              mount:
              "${kind}/${name}: container ${container.name or "?"} mounts hostPath volume "
              + "${mount.name} at ${mount.mountPath} with subPath ${mount.subPath}"
            ))
          ];
      in
      lib.concatMap offendingMounts containers;

  /*
    An env var with an empty string value never survives a round trip.

    `EnvVar.Value` is `json:"value,omitempty"` in the Kubernetes Go types, so
    the apiserver drops it:

      rendered   {"name": "PYNIXD_SSH_HOST", "value": ""}
      live       {"name": "PYNIXD_SSH_HOST"}

    Every GitOps tool then reports a difference between git and the cluster on
    a field that nobody can make match. Measured on nixlab2, where one such key
    held an ArgoCD Application OutOfSync for a whole session, and twice sent the
    reader after an unrelated unhealthy component first.

    The value belongs somewhere the apiserver does not rewrite. A string inside
    a ConfigMap is not an EnvVar and survives, which is where pynixd's
    `ssh_host` went.

    This is the same family as a rendered `kustomize = {}`: any field the
    apiserver drops must not be rendered.
  */
  emptyEnvIn =
    kind: name: resource:
    let
      pod = podSpecOf resource;
    in
    if pod == null then
      [ ]
    else
      let
        containers = (listOf pod "containers") ++ (listOf pod "initContainers");
        offending =
          container:
          lib.pipe (listOf container "env") [
            (lib.filter (entry: entry.value or null == ""))
            (map (
              entry:
              "${kind}/${name}: container ${container.name or "?"} sets ${entry.name} to an "
              + "empty string, which the apiserver drops"
            ))
          ];
      in
      lib.concatMap offending containers;

  # Walk every rendered resource with one of the checks above.
  walk =
    check:
    lib.pipe config.kubernetes.resources [
      (lib.mapAttrsToList (
        _namespace: kinds:
        lib.mapAttrsToList (
          kind: named: lib.mapAttrsToList (name: resource: check kind name resource) named
        ) kinds
      ))
      lib.flatten
    ];

  offenders = walk offendersIn;
  emptyEnvOffenders = walk emptyEnvIn;
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
      {
        assertion = emptyEnvOffenders == [ ];
        message =
          "An env var is set to an empty string. EnvVar.Value is "
          + "`json:\"value,omitempty\"`, so the apiserver drops it and the live "
          + "object never matches what was rendered. Every GitOps tool then "
          + "reports drift that nobody can resolve. Carry the value somewhere "
          + "the apiserver does not rewrite, such as a ConfigMap, or give it a "
          + "value that is not empty.\n  "
          + lib.concatStringsSep "\n  " emptyEnvOffenders;
      }
    ];
  };
}
