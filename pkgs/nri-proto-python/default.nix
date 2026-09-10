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
  nri = fetchFromGitHub {
    owner = "containerd";
    repo = "nri";
    rev = "1078130fa016884b4c03880d9d587e6691a67d98";
    sha256 = "sha256-E3UivHF+tTltMUrdgQk2rIJGtqOav4iqF1E3sYXsoGU=";
  };
in
buildPythonPackage {
  inherit version;
  pname = "nri-proto-python";

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
    mkdir -p $TMPDIR
    cp ${nri}/pkg/api/api.proto $TMPDIR/nri.proto

    mkdir -p src/nri
    touch src/nri/py.typed
    touch src/nri/__init__.py

    protoc \
      --proto_path="$TMPDIR" \
      --python_out="src/nri" \
      --grpclib_python_out="src/nri" \
      --mypy_out="src/nri" \
      nri.proto

    substituteInPlace src/nri/nri_grpc.py \
      --replace-fail "import nri_pb2" "from . import nri_pb2"
  '';

  meta = with lib; {
    description = "Python gRPC/protobuf library for containerd NRI (Node Resource Interface)";
    homepage = "https://github.com/containerd/nri";
    license = licenses.asl20;
    platforms = platforms.all;
  };
}
