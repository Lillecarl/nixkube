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
  version = "0.35.1";
  cri-api = fetchFromGitHub {
    owner = "kubernetes";
    repo = "cri-api";
    rev = "v${version}";
    sha256 = "sha256-Cgamp9z7XFsHfYA+BRoQ7Kb3v5d8/ueaYUreRNk2YI4=";
  };
in
buildPythonPackage {
  inherit version;
  pname = "cri-proto-python";

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
    cp ${cri-api}/pkg/apis/runtime/v1/api.proto $TMPDIR/cri.proto

    mkdir -p src/cri
    touch src/cri/py.typed
    touch src/cri/__init__.py

    protoc \
      --proto_path="$TMPDIR" \
      --python_out="src/cri" \
      --grpclib_python_out="src/cri" \
      --mypy_out="src/cri" \
      cri.proto

    substituteInPlace src/cri/cri_grpc.py \
      --replace-fail "import cri_pb2" "from . import cri_pb2"
  '';

  meta = with lib; {
    description = "Python gRPC/protobuf library for Kubernetes CRI (Container Runtime Interface)";
    homepage = "https://github.com/kubernetes/cri-api";
    license = licenses.asl20;
    platforms = platforms.all;
  };
}
