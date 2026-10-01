# How a pod sees the store

nixkube gives a pod a closure: a set of store paths. It can present that
closure in three ways. This page lists what each way costs, and which one
CSI and NRI use.

## The three presentations

**Bind farm.** One read-only bind mount for each store path in the closure.
A node's closure has about 2443 paths, so a pod gets about 2443 mounts.

**Hardlink tree.** One hardlink for each file in the closure, in a
directory on the node's disk. A node's closure has about 375,582 files.

**composefs.** An EROFS image holds the closure's metadata, and an overlay
mount reads the file contents from `/nix/store`. The image is a Nix
derivation (`nix/composefs.nix`), so a node can substitute an image that CI
or pynixd built. A pod gets one mount.

## Comparison

| | Bind farm | Hardlink tree | composefs |
| --- | --- | --- | --- |
| Mounts per pod | One per store path (about 2443) | One | One |
| Time to build | 14 us per path (about 34 ms) | 22 us per file (about 8 s) | One Nix build of the image, or a substitution |
| Disk | Nothing | A directory entry for each file | One image for each closure, shared by all pods |
| Kernel | `mount_setattr` (Linux 5.12) | Any | EROFS, and overlay data-only lower layers (Linux 6.5) |
| Limits | `fs.mount-max` is 100,000 for each mount namespace | The tree must be on the same filesystem as the store | None found |
| Host store on NixOS | Works | Fails: a link out of the host's store gives EXDEV | Works |
| `hostUsers: false` | Works | Works | Fails for CSI: the runtime cannot idmap an overlay |
| Kata and other VM runtimes | Fails: virtio-fs does not serve each bind | Works | Not tested |
| `subPath` | Fails: kubelet's `subPath` bind does not include submounts | Works | Works |
| Read-write | The gaps between the binds are writable, on the volume's disk | An overlay over the tree, upper directory on the volume's disk | The overlay's upper directory, on the volume's disk |
| Cleanup | The mounts go with the pod | Delete the tree | Unmount; the image stays until GC collects it |

Two more facts apply to the farm and the tree:

- A bind and a hardlink share the inode with `/nix/store`. A write through
  one changes the node's store. nixkube makes every bind read-only, and gives
  a read-write tree an overlay. composefs writes go to the overlay's upper
  directory, so the store is never written.
- overlayfs cannot use a bind farm as a lower layer. It does not follow
  submounts there, and it shows empty directories.

## CSI

nixkube tries composefs first, and uses a hardlink tree when composefs cannot
work. It decides for each volume:

1. `nixkube.csi.composefs = false`: a hardlink tree.
2. The kernel fails the probe (no EROFS, as on Talos 1.11 to 1.13 and GKE
   COS 125 and 129): a hardlink tree.
3. The pod has `hostUsers: false`: a hardlink tree. On a host-store node
   nixkube refuses the volume, because a hardlink tree cannot work there.
4. Otherwise: composefs.

The probe mounts a small image and accepts only an exact root. A kernel
that silently shows the whole base directory fails it.

CSI does not use the bind farm, because it breaks `subPath`.

## NRI

NRI puts `/nix` into a container after the runtime creates it, so no
runtime idmaps it and `subPath` does not apply.

1. A VM runtime (Kata): a hardlink tree, served through virtio-fs.
2. A host-store node: the bind farm.
3. Otherwise: the bind farm. `NRI_BIND_FARM=false` gives a hardlink tree.

NRI on composefs is not implemented yet.
