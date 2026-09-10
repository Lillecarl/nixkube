# SPDX-License-Identifier: MIT

# Credits to Claude Sonnet 4.6
{
  lib,
  buildPythonPackage,
  fetchFromGitHub,
  hatchling,
  grpcio-tools,
  grpclib,
  protobuf,
  mypy-protobuf,
}:
let
  version = "0.11.0";
  ttrpc = fetchFromGitHub {
    owner = "containerd";
    repo = "ttrpc";
    rev = "v1.2.7";
    sha256 = "sha256-oQamR59cQrcuw9tervKrf+2vYnweRRNgST8GObFNjTk=";
  };
in
buildPythonPackage {
  inherit version;
  pname = "ttrpc-proto-python";

  src = lib.cleanPythonSource ./.;

  build-system = [ hatchling ];
  nativeBuildInputs = [
    grpclib
    grpcio-tools
    mypy-protobuf
  ];

  dependencies = [
    grpclib
    protobuf
  ];

  format = "pyproject";
  preBuild = ''
    mkdir -p $TMPDIR/proto
    cp ${ttrpc}/request.proto $TMPDIR/ttrpc.proto
    cp ${ttrpc}/proto/status.proto $TMPDIR/proto/status.proto
    cp ${ttrpc}/integration/streaming/test.proto $TMPDIR/streaming.proto

    mkdir -p src/ttrpc/proto
    touch src/ttrpc/py.typed
    touch src/ttrpc/__init__.py
    touch src/ttrpc/proto/__init__.py

    protoc \
      --proto_path=$TMPDIR \
      --python_out=src/ttrpc \
      --grpclib_python_out=src/ttrpc \
      --mypy_out=src/ttrpc \
      ttrpc.proto proto/status.proto streaming.proto

    # Fix relative imports in generated code
    substituteInPlace src/ttrpc/ttrpc_pb2.py \
      --replace-fail "from proto import status_pb2" "from .proto import status_pb2"
  '';

  meta = with lib; {
    description = "Python gRPC/protobuf library for ttRPC";
    homepage = "https://github.com/containerd/ttrpc";
    license = licenses.asl20;
    platforms = platforms.all;
  };
}
