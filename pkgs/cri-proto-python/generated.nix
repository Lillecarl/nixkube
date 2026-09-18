# SPDX-License-Identifier: MIT

{
  fetchFromGitHub,
  protoGenerated,
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
protoGenerated {
  inherit version;
  projectRoot = ./.;
  package = "cri";
  prepareProtos = ''
    cp ${cri-api}/pkg/apis/runtime/v1/api.proto "$protoDir/cri.proto"
  '';
  protoFiles = [ "cri.proto" ];
  fixImports = ''
    substituteInPlace "$out/src/cri/cri_grpc.py" \
      --replace-fail "import cri_pb2" "from . import cri_pb2"
  '';
}
