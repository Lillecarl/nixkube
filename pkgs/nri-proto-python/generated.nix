# SPDX-License-Identifier: MIT

{
  fetchFromGitHub,
  protoGenerated,
}:

let
  nri = fetchFromGitHub {
    owner = "containerd";
    repo = "nri";
    rev = "1078130fa016884b4c03880d9d587e6691a67d98";
    sha256 = "sha256-E3UivHF+tTltMUrdgQk2rIJGtqOav4iqF1E3sYXsoGU=";
  };
in
protoGenerated {
  version = "0.11.0";
  projectRoot = ./.;
  package = "nri";
  prepareProtos = ''
    cp ${nri}/pkg/api/api.proto "$protoDir/nri.proto"
  '';
  protoFiles = [ "nri.proto" ];
  fixImports = ''
    substituteInPlace "$out/src/nri/nri_grpc.py" \
      --replace-fail "import nri_pb2" "from . import nri_pb2"
  '';
}
