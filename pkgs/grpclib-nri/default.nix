# SPDX-License-Identifier: MIT

{
  buildPythonPackage,
  buildGoModule,
  hatchling,
  grpclib-ttrpc,
  lib,
  nri-proto-python,
  pytestCheckHook,
  pytest-asyncio,
  structlog,
  typing-extensions,
}:
let
  pyproject = builtins.fromTOML (builtins.readFile ./pyproject.toml);

  testServer = buildGoModule {
    pname = "nri-test-server";
    version = "0.1.0";

    src = lib.cleanSource ./go;
    proxyVendor = true;
    vendorHash = "sha256-bpKT8mHSlA5eP67C3B2ws+BF2S/B+dMH5GYqV2edcXg=";
    doCheck = false;

    ldflags = [
      "-s"
      "-w"
    ];

    meta.mainProgram = "grpclib-nri-test-server";
  };
in
buildPythonPackage {
  pname = pyproject.project.name;
  version = pyproject.project.version;

  src = lib.cleanPythonSource ./.;
  pyproject = true;
  build-system = [ hatchling ];

  dependencies = [
    grpclib-ttrpc
    nri-proto-python
    structlog
    typing-extensions
  ];

  nativeCheckInputs = [
    pytestCheckHook
    pytest-asyncio
    testServer
  ];

  preCheck = ''
    export NRI_TEST_SERVER="${lib.getExe testServer}"
  '';

  passthru = {
    inherit testServer;
  };
}
