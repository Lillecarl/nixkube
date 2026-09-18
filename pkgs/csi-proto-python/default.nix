# SPDX-License-Identifier: MIT

# Credits to Claude Sonnet 3.7
{
  lib,
  buildPythonPackage,
  fetchFromGitHub,
  hatchling,
  grpcio-tools,
  grpcio,
  grpclib,
  protobuf,
  mypy-protobuf,
  python,
  pythonRelaxDepsHook,
  # The C++ protobuf, which carries `bin/protoc`. The `protobuf` above is the
  # Python package and has no binary, so the two are not interchangeable.
  protoc,
}:
let
  version = "1.11.0";
  spec = fetchFromGitHub {
    owner = "container-storage-interface";
    repo = "spec";
    rev = "v${version}";
    sha256 = "sha256-mDvlHB2vVqJIQO6y2UJlDohzHUbCvzJ9hJc7XFAbFb0=";
  };
in
buildPythonPackage {
  inherit version;
  pname = "csi-proto-python";

  src = lib.cleanPythonSource ./.;

  build-system = [ hatchling ];
  # Build tooling only. `protoc` and its two plugins run here and are not
  # imported by anything the consumer loads, so they must not be propagated.
  nativeBuildInputs = [
    protoc
    grpclib
    mypy-protobuf
    grpcio-tools
  ];

  # What the generated modules import at runtime, and nothing else.
  dependencies = [
    grpclib
    protobuf
  ];

  format = "pyproject";
  preBuild = ''
    mkdir -p src/csi
    touch src/csi/py.typed
    touch src/csi/__init__.py
    protoc \
      --proto_path="${spec}" \
      --python_out="src/csi" \
      --grpclib_python_out="src/csi" \
      --mypy_out="src/csi" \
      csi.proto

    substituteInPlace src/csi/csi_grpc.py \
      --replace-fail "import csi_pb2" "from . import csi_pb2"
  '';

  meta = with lib; {
    description = "Python gRPC/protobuf library for Kubernetes CSI spec";
    homepage = "https://github.com/container-storage-interface/spec";
    license = licenses.asl20;
    platforms = platforms.all;
  };
}
