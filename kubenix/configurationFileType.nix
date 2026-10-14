/**
  Accept a file package or a pair containing its exact preview text.
*/
{ lib }:
lib.types.coercedTo lib.types.package
  (file: {
    inherit file;
    text = null;
  })
  (
    lib.types.submodule {
      options = {
        file = lib.mkOption {
          type = lib.types.package;
          description = "Validated configuration file.";
        };
        text = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          description = "Exact file contents for preview, or null to read the file.";
        };
      };
    }
  )
