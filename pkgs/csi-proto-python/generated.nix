# SPDX-License-Identifier: MIT

{
  fetchFromGitHub,
  protoGenerated,
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
protoGenerated {
  inherit version;
  projectRoot = ./.;
  package = "csi";
  prepareProtos = ''
    cp ${spec}/csi.proto "$protoDir/csi.proto"
  '';
  protoFiles = [ "csi.proto" ];
  fixImports = ''
    substituteInPlace "$out/src/csi/csi_grpc.py" \
      --replace-fail "import csi_pb2" "from . import csi_pb2"
  '';
}
