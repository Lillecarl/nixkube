# SPDX-License-Identifier: MIT

{
  buildPythonApplication, # Builder
  dockerTools, # binSh, caCertificates, usrBinEnv for container setup
  hatchling, # Build system
  coreutils, # ln
  cryptography, # ssh-keygen Python
  cri-proto-python, # CRI gRPC bindings
  csi-proto-python, # CSI gRPC bindings
  nri-proto-python, # NRI ttRPC bindings
  grpclib-nri, # NRI protocol utilities
  googleapis-common-protos, # Google Errors
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
  src = lib.cleanSource ./.;
  pyproject = true;
  build-system = [ hatchling ];
  dependencies = [
    coreutils
    cryptography
    cri-proto-python
    csi-proto-python
    nri-proto-python
    grpclib-nri
    googleapis-common-protos
    gitMinimal
    kr8s
    shellous
    nix
    nix_init_db
    openssh
    util-linuxMinimal
    pyzmq
    nri-wait
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
