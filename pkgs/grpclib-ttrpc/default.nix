# SPDX-License-Identifier: MIT

{
  buildPythonPackage,
  buildGo125Module,
  hatchling,
  grpclib,
  multidict,
  mypy-protobuf,
  grpcio-tools,
  protobuf,
  structlog,
  ttrpc-proto-python,
  pytestCheckHook,
  pytest-asyncio,
  lib,
}:
let
  pyproject = builtins.fromTOML (builtins.readFile ./pyproject.toml);

  testServer = buildGo125Module {
    pname = "ttrpc-test-server";
    version = "0.1.0";
    src = lib.cleanSource ./go;
    proxyVendor = true;
    vendorHash = "sha256-voE9iZ0rUp/iCNROLiKjuQdQS9rLVqPK0SlSGp0kPuU=";
    doCheck = false;

    ldflags = [
      "-s"
      "-w"
    ];

    meta.mainProgram = "grpclib-ttrpc-test-server";
  };
in
buildPythonPackage {
  pname = pyproject.project.name;
  version = pyproject.project.version;

  src = lib.cleanPythonSource ./.;
  pyproject = true;
  build-system = [ hatchling ];

  dependencies = [
    protobuf
    grpclib
    multidict
    structlog
    ttrpc-proto-python
  ];

  nativeBuildInputs = [
    protobuf
    grpclib
    mypy-protobuf
    grpcio-tools
  ];

  nativeCheckInputs = [
    pytestCheckHook
    pytest-asyncio
    testServer
  ];

  preCheck = ''
    export TTRPC_TEST_SERVER="${lib.getExe testServer}"
  '';

  passthru = {
    inherit testServer;
  };
}
