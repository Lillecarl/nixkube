# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Version Control

**Always use Jujutsu (jj) instead of Git** for all version control operations in this repository.

**Before committing**: Run `just precommit` (formats, lints, tests, and regenerates docs). This is the single command to run before every `jj commit`. Do not run `just fmt`, `just lint`, `just test`, or `just gendoc` individually when preparing a commit — use `just precommit` instead.

**Running `just` recipes**: You do NOT need `direnv exec . ` to run `just` recipes—they're already part of the dev environment. Only use `direnv exec . ` when running Python introspection code (e.g., `direnv exec . python3 -c ...` or `direnv exec . python3 << 'EOF'`) that depends on rebuilt dependencies in Nix.

## Project Overview

nixkube is a Kubernetes plugin system that injects Nix stores into pods using two complementary protocols:

1. **CSI (Container Storage Interface)** - Ephemeral volumes with explicit pod requests via volumeAttributes
2. **NRI (Node Resource Interface)** - Container runtime hooks for automatic injection via pod annotations

The system consists of:

1. **Node DaemonSet** - Runs on each Kubernetes node, implements both CSI and NRI protocols to mount Nix stores into pods
2. **Cache StatefulSet** - Central cache/coordinator that manages distributed builds and binary substitution
3. **Optional Builder Pods** - For offloading builds to dedicated builder nodes

The CSI layer supports two driver names for backwards compatibility:
- **nixkube** - Primary driver name (recommended for new deployments)
- **nix.csi.store** - Legacy driver name (enabled via `cfg.node.compat`)

## Architecture

### Build System

This repository is not a flake. `nix/sources.nix` asks the nixidae umbrella where every dependency lives, and `default.nix` takes that set as `sources`. Build with `nix build --file . <attribute>`. Key build outputs are defined in `default.nix`:

- **Environments** (`environments/`): Separate Nix environments for cache and node, built using dinix (a service manager). Each environment:
  - Shares common services (openssh, nix-daemon, shared-setup)
  - Has role-specific services defined in separate modules
  - Builds for both x86_64-linux and aarch64-linux architectures
  - Is deployed as a minimal container with services managed by dinit

- **Kubernetes Deployment** (`kubenix/`): Uses easykubenix to generate Kubernetes manifests
  - `options.nix`: Defines module options and builds separate `cachePackage` and `nodePackage`
  - `cache.nix`: Cache StatefulSet with initContainer that copies environment artifacts
  - `daemonset.nix`: Node DaemonSet with CSI driver registration
  - SSH keys from `./keys/*.pub` are automatically imported as authorized keys

### Communication Flow

**Cache → Nodes**: The cache service watches for pods labeled `app.kubernetes.io/component=node` and updates `/etc/machines` with builder DNS names (`pod.name.nixkube-builders.namespace.svc.cluster.local`). This enables distributed builds.

**Nodes → Cache**: Node pods use the cache as a binary substitute via `ssh-ng://nix@pynixd?trusted=1` configured in `kubenix/config.nix`.

**CSI Protocol**: When a pod requests a volume with `storePath` or `nixExpr` or `flakeRef` in volumeAttributes, the node CSI driver:
1. Builds/fetches the requested Nix store path
2. Copies artifacts to the cache (if configured)
3. Mounts the store path into the pod using bind mounts

### Python Service Architecture

The nixkube service is a single multi-protocol daemon running both CSI and NRI servers concurrently:

**CSI Server** (`src/csi/`):
- `grpclib` for async gRPC protocol implementation
- `csi-proto-python` for CSI protobuf definitions (upstream spec)
- Handles explicit ephemeral volume requests via volumeAttributes
- Supports both nixkube and nix.csi.store driver names for backwards compatibility

**NRI Plugin** (`src/nri/`):
- ttrpc (transport-agnostic RPC) for NRI protocol
- Pod annotation parsing for automatic store injection
- OCI hook invocation for container initialization
- ZeroMQ-based coordination with build tasks

**Shared Infrastructure**:
- `kr8s` for Kubernetes API interactions and event reporting
- `nri-wait` for container initialization coordination
- Subprocess orchestration for Nix operations

Entry point (`src/cli.py`) starts both servers and features:
- Configurable logging via `/etc/nix/logging.json` ConfigMap
- Environment variable controls for feature flags (ENABLE_COMPAT_DRIVER, NRI_ENABLED)
- Graceful multi-server error handling

## Common Commands

Use the `justfile` for common development tasks. Run `just --list` to see all available recipes.

### Code Quality

**Format code before committing:**
```bash
just fmt
```

**Check formatting without changes:**
```bash
just check-fmt
```

**Run Python type checker:**
```bash
just lint
```

### Testing

**Run Python tests:**
```bash
just test
```

The integration test (runs in CI via `.github/workflows/integration-test.yaml`):
- Checks `/nix/store` is accessible in test pods
- Validates CSI driver registration
- Confirms cache and node pods are operational

**Build job** (runs once, pushes to cachix and container registry):
1. Builds and pushes Nix image
2. Builds and pushes cache/node environments
3. Builds and pushes scratch image

**Test jobs** (can run in parallel, pull from caches):
- `test-kind`: Tests deployment on Kind cluster using `kubenixApply` with `local="true"`
- Future test jobs can be added for different deployment scenarios (e.g., different K8s versions, configurations)

### Building

**Build Kubernetes manifests:**
```bash
just build-manifests
```

**Build development environment:**
```bash
just build-dev
```

**Build all outputs for both architectures (requires builders):**
```bash
just build-all
```

### Deployment

**Deploy to Kubernetes cluster** (reads SSH keys from `./keys/*.pub`):
```bash
just deploy
```

**Push to cachix and container registry:**
```bash
just push
```

### Development Environment

The development environment includes:
- Python with nixkube, csi-proto-python, kr8s
- xonsh shell
- ruff, pyright (linting/type checking)
- kluctl, stern, kubectx (Kubernetes tools)
- buildah, skopeo, regctl (container tools)
- just (for running recipes)

Enter the environment with `direnv allow && direnv reload` (or open a new terminal).

### Python Development

The Python code is in `pkgs/nixkube/src/`:
- `src/csi/server.py` - CSI driver (gRPC NodeServicer implementation)
- `src/nri/server.py` - NRI plugin (ttrpc handler implementation)
- `src/cli.py` - Service entry point that runs both CSI and NRI servers

Version is managed in `pkgs/nixkube/pyproject.toml` and automatically imported into the Nix build.

## Key Configuration Points

### Volume Attributes

Pods request Nix stores via CSI volumeAttributes (see README for examples):
- `storePath`: Direct Nix store path to mount
- `nixExpr`: Nix expression to evaluate and mount
- `flakeRef`: Flake reference to build and mount

### Service Dependencies (dinit)

Both environments use dinit for service management with dependency chains:
- Cache: `shared-setup` → `nix-daemon` → `cache-daemon` → `cache-logger` → `cache` (umbrella)
- Node: `shared-setup` → `nix-daemon` → `csi-gc` → `csi-daemon` → `csi-logger` → `csi` (umbrella)

The `config-reconciler` service runs continuously to sync SSH keys and Nix config from mounted volumes to runtime locations.

## Important Files

- `default.nix`: Main entry point, defines all build outputs
- `environments/cache/default.nix`: Cache environment with shared + cache services
- `environments/node/default.nix`: Node environment with shared + CSI services
- `kubenix/options.nix`: Kubernetes module options and package builds
- `kubenix/daemonset.nix`: Node DaemonSet with CSI/NRI containers and registration
- `kubenix/cache.nix`: Cache StatefulSet with build coordination services
- `kubenix/builder.nix`: Optional builder pods for distributed builds
- `pkgs/nixkube/src/csi/server.py`: CSI driver gRPC server
- `pkgs/nixkube/src/nri/server.py`: NRI plugin ttrpc handler
- `niximage.nix`: Builds the Nix container used by initContainers

## Code Review Standards

**The code should be approachable for both AI and beginners.** When reviewing or modifying code, follow these guidelines for comments:

### When Comments Are Useful
- ✅ **Non-obvious design decisions**: Why a particular approach was chosen over alternatives
- ✅ **Complex algorithms**: Explain the logic when it's not immediately clear from the code
- ✅ **Protocol requirements**: CSI interface requirements, Kubernetes API quirks, Nix behavior
- ✅ **Error handling philosophy**: Why fail-fast vs. graceful degradation (see `NodeUnpublishVolume` for good example)
- ✅ **Performance implications**: Why bind mounts vs. overlayfs, concurrency limits, retry strategies
- ✅ **Footguns and gotchas**: Things that could easily be misunderstood or break

### When Comments Are Noise (Avoid These)
- ❌ **Restating what's already obvious**: Variable names, function names, and code structure should be self-documenting
- ❌ **Duplicating log messages**: Log messages serve as inline documentation - don't repeat them in comments
- ❌ **Explaining standard patterns**: `exp_backoff`, `async with lock`, walrus operators, set comprehensions - trust the reader
- ❌ **Obvious control flow**: for-else with clear error logging, early returns, simple conditionals

### Python Logging

This project uses **structlog**. All log calls use a plain event string plus structured kwargs — no f-string interpolation in the event itself:

```python
# ✅ Good — plain event + kwargs
logger.info("fetched_packages", count=n, store=str(path))
logger.debug("build_task_started", container_id=container_id, count=len(store_paths))

# ❌ Avoid — f-strings in event, or % formatting
logger.info(f"Fetched {n} packages from {path}")
logger.info("Fetched %d packages", n)
```

**Exception logging:**
- Inside `except` blocks that re-raise: `logger.exception("event_name")` — shorthand for ERROR + full traceback
- Inside `except` blocks that swallow: `logger.warning("event_name", exc_info=True)` — WARNING + full traceback
- In callbacks with an exception object (not in `except`): `logger.error("event_name", exc_info=t.exception())`

**Context binding** — attach per-request metadata to a logger:
```python
log = structlog.get_logger("nixkube.nri.buildtask").bind(container_id=container_id)
log.info("build_task_started")  # container_id included in every call
```

### Testing Philosophy
- **Integration tests over unit tests**: This project orchestrates subprocess calls to Nix, rsync, mount, etc. Unit testing these would require excessive mocking and wouldn't catch real issues.
- **Test in real environments**: Use the GitHub Actions integration tests on actual Kind clusters to validate behavior.
- Unit tests are only valuable for pure business logic isolated from subprocess orchestration.
