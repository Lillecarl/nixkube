# NixOS Integration Tests for nix-csi

Replace GitHub Actions image-based testing with reproducible, version-controlled NixOS tests. Test nix-csi against real kubeadm clusters with containerd and crio runtimes.

## Goals

1. **Eliminate GHA image brittleness** — All cluster setup defined in Nix, reproducible locally
2. **Comprehensive CRI coverage** — Test both containerd and crio runtimes
3. **Full deployment scenarios** — CSI (ro/rw/multimount) + NRI (ro/rw/multimount) + cache coordination
4. **Self-documenting** — Test code IS the deployment spec

## Architecture

### Test Structure

```
tests/nixos/
├── integration.nix          # Main test definition
├── cluster-module.nix       # Reusable kubeadm cluster config
└── test-scenarios.nix       # Workload/assertion helpers
```

### Single Test = Two Clusters

Each test run spins up two independent single-node kubeadm clusters:
- `kubeadm-containerd` — Control plane + worker, containerd runtime
- `kubeadm-crio` — Control plane + worker, crio runtime

Both clusters deployed with nix-csi, cache, and builder pods. Tests run identically on both.

## Implementation Plan

### Phase 1: Basic Containerd Cluster (MVP)
**Goal**: Single working kubeadm + containerd cluster with nix-csi, verify pod startup

**What to build**:
1. Adapt hetzkube modules for test VMs:
   - Reuse `kubernetes.nix` (kubelet + containerd config)
   - Simplify `networking.nix` (nixosTest has built-in networking)
   - Skip `disko.nix` (tests use tmpfs by default)
   - Minimal `nix.nix` (just enable nix-daemon)

2. Create `cluster-module.nix`:
   ```nix
   { cri ? "containerd" }:
   { config, pkgs, lib, ... }:
   {
     imports = [
       ./kubernetes.nix
       # conditionally import crio.nix when cri == "crio"
     ];

     # Kubelet + CRI systemd units (from kubernetes.nix)
     # CNI config for bridge-cni (just write config file)
     environment.etc."cni/net.d/10-bridge.conflist".text = ''
       {
         "cniVersion": "1.0.0",
         "name": "bridge",
         "plugins": [
           {"type": "bridge"},
           {"type": "host-local", "ranges": [{"subnet": "10.244.0.0/16"}]}
         ]
       }
     '';
   }
   ```

3. Create `integration.nix` test with:
   ```nix
   {
     name = "nix-csi-kubeadm-containerd";
     nodes.kubeadm-containerd = {
       imports = [ ./cluster-module.nix ];
       # config...
     };

     testScript = ''
       kubeadm-containerd.start()
       kubeadm-containerd.wait_for_unit("kubelet")

       # Deploy nix-csi manifests via kubectl
       # Create test pod with CSI volume
       # Verify /nix/store mounted
     '';
   }
   ```

4. **kubeadm init** — Imperative in testScript:
   ```bash
   kubeadm init \
     --skip-phases=addon/kube-proxy \
     --pod-network-cidr=10.244.0.0/16
   mkdir -p /root/.kube
   cp /etc/kubernetes/admin.conf /root/.kube/config
   ```

5. **Manifest deployment** — Bake kubenixApply into VM image:
   - Build `kubenixApply.deploymentScript` in flake
   - Include in VM's `environment.systemPackages`
   - Execute in testScript after kubeadm cluster ready:
     ```bash
     /run/current-system/sw/bin/kubenix-deploy --kubeconfig=/root/.kube/config
     ```
   - This is cleaner than manual mounting and keeps everything declarative

6. **Success criteria for Phase 1**:
   - ✅ Cluster reaches Ready state
   - ✅ nix-csi DaemonSet Running on worker
   - ✅ Pod with ephemeral CSI volume starts
   - ✅ `/nix/store` accessible in pod
   - ✅ Simple workload can run (e.g., `nix --version` in pod)

### Phase 2: Expand Test Scenarios
**Goal**: CSI (ro/rw/multimount) + cache + builder coordination

**Includes builder pods** to test distributed build coordination.

**Add tests**:
1. **CSI ro mount** — Ephemeral volume, read-only
2. **CSI rw mount** — Ephemeral volume, read-write
3. **CSI multimount** — Pod mounts multiple nix stores
4. **Cache coordination** — Verify cache pod receives and services requests from nodes
5. **Builder pods** — Verify builder pods receive build tasks from nodes
6. **Pod cleanup** — Verify volumes cleaned up on pod deletion

### Phase 3: NRI Support
**Goal**: Same scenarios via NRI pod annotations

**Add**:
1. NRI plugin registration & lifecycle
2. Pod annotation-based volume injection (ro/rw/multimount)
3. OCI hook invocation verification
4. Cache coordination for NRI builds

### Phase 4: crio Runtime
**Goal**: Identical test suite for crio

**What changes**:
- Conditional: `cri = "crio"` in cluster-module
- Configure crio instead of containerd
- Verify same CSI/NRI scenarios work identically

## Reuse from hetzkube

| Component | hetzkube | nix-csi test | Adaptation |
|-----------|----------|--------------|------------|
| kubelet config | `kubernetes.nix` | Reuse | Minimal flags only |
| containerd CRI | `kubernetes.nix` | Reuse | No changes needed |
| CNI plugins | cilium | Replace | Use bridge-cni or simple alternative |
| systemd-networkd | `networking.nix` | Simplify | nixosTest provides basic networking |
| nix-daemon | `nix.nix` | Simplify | Just `enable = true` |
| Disk setup | `disko.nix` | Skip | tmpfs is fine for tests |
| SSH/cloud-init | Not needed | Skip | nixosTest has native SSH |

## Key Implementation Details

### Mounting Manifests
```nix
# In flake.nix
{
  outputs = { self, nixpkgs, ... }:
    let
      nix-csi-manifests = self.packages.${system}.kubernetes-manifests;
    in {
      checks.${system}.nixos-integration = nixpkgs.legacyPackages.${system}.nixosTest {
        virtualisation.additionalPaths = [ nix-csi-manifests ];
        # In testScript: ${nix-csi-manifests}/manifest.yaml
      };
    };
}
```

### kubeadm init in testScript
```bash
# Runs as root in VM
kubeadm init \
  --skip-phases=addon/kube-proxy \
  --pod-network-cidr=10.244.0.0/16 \
  --node-name=control

mkdir -p /root/.kube
cp /etc/kubernetes/admin.conf /root/.kube/config

# Remove control-plane label so nix-csi DaemonSet runs on this node
kubectl taint nodes control node-role.kubernetes.io/control-plane:NoSchedule-

# Wait for cluster ready
kubectl --kubeconfig=/root/.kube/config wait --for=condition=Ready node/control --timeout=60s
```

### Storage for Cache Pod
Deploy `local-path-provisioner` immediately after cluster bootstrap:
```bash
kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/master/deploy/local-path-storage.yaml
# Configure cache StatefulSet to use local-path storage class
```

Cache pod will claim PVCs for:
- Binary cache storage (`/var/cache/nix`)
- State directory (`/var/lib/nix-csi`)

### Control-Plane Label Warning
**CRITICAL**: Single-node test clusters have control-plane taint by default. nix-csi DaemonSet must run on all nodes:
1. Remove taint after kubeadm init: `kubectl taint nodes control node-role.kubernetes.io/control-plane:NoSchedule-`
2. DaemonSet can now schedule on control node
3. Without this, nix-csi never runs and all CSI operations fail

### Pod Verification
```python
# In testScript - using ghcr.io/nixidae/nixkube/scratch:1.0.1
machine.succeed("kubectl create -f /nix/store/.../test-pod.yaml")
machine.wait_until_succeeds("kubectl get pod test-pod -o jsonpath='{.status.phase}' | grep Running")
machine.succeed("kubectl exec test-pod -- test -d /nix/store")
machine.succeed("kubectl exec test-pod -- ls -la /nix/store")
machine.succeed("kubectl exec test-pod -- nix --version")
```

## Running Tests

### Locally (before pushing)
```bash
# Phase 1 only (containerd)
nix flake check --assert 'nixos-integration-containerd'

# All phases
nix flake check
```

### In CI (replaces current GHA test jobs)
```bash
nix flake check --override-input nixpkgs github:nixos/nixpkgs/nixos-unstable
```

## Success Criteria

### Phase 1 (MVP)
- [ ] Cluster bootstraps cleanly with kubeadm
- [ ] nix-csi DaemonSet + StatefulSet running
- [ ] CSI ephemeral volume mount succeeds
- [ ] Workload can execute inside pod with /nix/store accessible
- [ ] Pod deletion cleans up volumes
- [ ] Test is reproducible: `nix flake check` works identically locally & in CI

### Phase 2
- [ ] CSI ro/rw/multimount all pass
- [ ] Cache pod receives and services requests
- [ ] Binary substitution from cache works

### Phase 3
- [ ] NRI plugin registers and receives lifecycle events
- [ ] Pod annotation injection works (ro/rw/multimount)
- [ ] OCI hooks invoked correctly
- [ ] NRI build coordination functional

### Phase 4
- [ ] crio runtime cluster bootstraps identically
- [ ] All CSI + NRI scenarios pass on crio
- [ ] No behavior differences between containerd and crio

## Decisions Made

1. **CNI choice**: bridge-cni (simple, minimal dependencies)
2. **kubelet flags**: Minimal, as few as possible
3. **Builder pods**: Yes, include in cluster for Phase 2+ tests
4. **Test pod images**: `ghcr.io/nixidae/nixkube/scratch:1.0.1`
5. **Manifest generation**: Bake `kubenixApply.deploymentScript` into VM, execute post-kubeadm
6. **Storage provisioner**: local-path-provisioner for cache pod PVCs
7. **Control-plane taints**: Remove `node-role.kubernetes.io/control-plane:NoSchedule-` after kubeadm init

## Deployment Sequencing with easykubenix

Deploy in 4 phases using separate easykubenix invocations, each with `kubectl wait` for readiness:

```bash
# Phase 1: kubeadm bootstrap
kubeadm init ...
kubectl wait --for=condition=Ready node/control --timeout=60s

# Phase 2: Cluster essentials (local-path-provisioner, networking)
KUBECONFIG=/root/.kube/config kubenix-deploy-essentials
kubectl wait --for=condition=Ready -l app=local-path-provisioner pod --timeout=30s

# Phase 3: nix-csi (DaemonSet, cache StatefulSet, builder pods)
KUBECONFIG=/root/.kube/config kubenix-deploy-nixkube
kubectl wait --for=condition=Running -l app.kubernetes.io/component=node pod --timeout=60s
kubectl wait --for=condition=Running -l app.kubernetes.io/component=cache pod --timeout=60s

# Phase 4: Test workloads (test pods with CSI/NRI volumes)
KUBECONFIG=/root/.kube/config kubenix-deploy-tests
kubectl wait --for=condition=Running pod/test-csi --timeout=60s
kubectl wait --for=condition=Running pod/test-nri --timeout=60s
```

This approach:
- ✅ Minimal wait times (kubectl wait triggers immediately on readiness)
- ✅ Sequential deployment ensures dependencies are met
- ✅ Each phase can be tested/debugged independently
- ✅ Clear separation of concerns (bootstrap → essentials → nix-csi → tests)

## Files to Create

```
tests/nixos/
├── integration.nix          # Main test, imports cluster-module for containerd
├── integration-crio.nix     # Test variant for crio runtime (Phase 4)
├── cluster-module.nix       # Reusable cluster config (containerd + crio)
└── test-scenarios.nix       # Shared test helpers and assertions

flake.nix changes:
  - Add `checks.nixos-integration-containerd` (Phase 1)
  - Add `checks.nixos-integration-crio` (Phase 4)
  - Ensure kubenixApply outputs are accessible to test modules
```

## Debugging Strategy (Future Enhancement)

For test failures, optionally launch upterm session with preconfigured SSH keys:

```bash
# In testScript, on failure:
if test_failed:
  machine.succeed("upterm host --server=wss://upterm.example.com -- bash")
  # User can SSH in and inspect cluster state
```

Allows interactive debugging instead of dumping logs. Requires:
- upterm server (can be self-hosted)
- SSH public keys configured in test VM
- Optional flag to enable (e.g., `--debug-on-failure`)

**Status**: Not needed for MVP, add in Phase 2+ if useful.

## Next Steps

1. **Create kubenix modules**:
   - `kubenix/test-essentials.nix` (local-path-provisioner, CNI)
   - `kubenix/test-workloads.nix` (CSI and NRI test pods)
   - Each deployable via separate easykubenix invocation

2. **Create cluster-module.nix** — containerd + kubelet systemd units, rest as static pods

3. **Write Phase 1 test** — Get kubeadm bootstrap → essentials → nixkube → tests working

4. **Test locally** — `nix flake check` should work end-to-end

5. **Add crio variant** — Test both runtimes (Phase 4)
