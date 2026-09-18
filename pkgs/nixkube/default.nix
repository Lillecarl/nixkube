# SPDX-License-Identifier: MIT

{
  anyio, # Structured concurrency, and non-blocking file IO in async code
  buildPythonApplication, # Builder
  dockerTools, # binSh, caCertificates, usrBinEnv for container setup
  hatchling, # Build system
  coreutils, # ln
  cri-proto-python, # CRI gRPC bindings
  csi-proto-python, # CSI gRPC bindings
  nri-proto-python, # NRI ttRPC bindings
  grpclib, # GRPCError and grpclib.client, imported directly
  protobuf, # google.protobuf.wrappers_pb2, imported directly
  grpclib-nri, # NRI protocol utilities
  gitMinimal,
  kr8s, # Kubernetes API
  shellous, # subprocessing
  nix,
  nix_init_db, # Import from one nix DB to another
  openssh, # Copying to cache
  lib,
  util-linuxMinimal, # mount, umount
  pyzmq, # Talking to OCI hooks
  nri-wait, # OCI hook for waiting on NRI builds
  prometheus-client, # The /metrics endpoint
  structlog, # Structured logging library
  rich, # Rich terminal output (used for structlog RichTracebackFormatter)
  pytest, # Unit tests
  pytest-asyncio, # Async test support
  hypothesis, # Property-based testing
  pytestCheckHook, # Runs them
}:
let
  pyproject = builtins.fromTOML (builtins.readFile ./pyproject.toml);
in
buildPythonApplication {
  pname = pyproject.project.name;
  version = pyproject.project.version;
  src = lib.cleanPythonSource ./.;
  pyproject = true;
  build-system = [ hatchling ];
  dependencies = [
    anyio
    coreutils
    cri-proto-python
    csi-proto-python
    nri-proto-python
    grpclib
    protobuf
    grpclib-nri
    gitMinimal
    kr8s
    shellous
    # **`lib.getBin`, not the package.** A Python package's `dependencies` are
    # `propagatedBuildInputs`, and a multi-output C++ package propagated that
    # way brings its `dev` output. `nix` brought `nix-2.34.8-dev`, and with it
    # boost's headers (147 MiB) and perl (55 MiB). The daemon runs the
    # binaries and reads none of the headers.
    (lib.getBin nix)
    nix_init_db
    (lib.getBin openssh)
    (lib.getBin util-linuxMinimal)
    pyzmq
    nri-wait
    prometheus-client
    structlog
    rich
  ];
  # pytest and its plugins were here already, with nothing to run them, so
  # `nix build` never saw a test result. The hook is what makes the list mean
  # something.
  nativeCheckInputs = [
    pytest
    pytest-asyncio
    hypothesis
    pytestCheckHook
  ];
  makeWrapperArgs = [
    "--set"
    "SETUP_BINSH"
    "${dockerTools.binSh}"
    "--set"
    "SETUP_CACERTS"
    "${dockerTools.caCertificates}"
    "--set"
    "SETUP_USRBINENV"
    "${dockerTools.usrBinEnv}"
  ];
  meta.mainProgram = "nixkube";
}
