# SPDX-License-Identifier: MIT
"""Import every module of each package named on the command line.

Stands in for nixpkgs' `pythonImportsCheck`, which pyproject.nix's builders
have no equivalent of. See nix/tests.nix for why a walk rather than a list.
"""

import importlib
import pkgutil
import sys


def main() -> int:
    failed: list[tuple[str, BaseException]] = []
    count = 0

    for name in sys.argv[1:]:
        package = importlib.import_module(name)
        count += 1
        for module in pkgutil.walk_packages(package.__path__, f"{name}."):
            try:
                importlib.import_module(module.name)
            except BaseException as error:  # noqa: BLE001 - report them all
                failed.append((module.name, error))
            else:
                count += 1

    for module, error in failed:
        print(f"{module}: {type(error).__name__}: {error}", file=sys.stderr)

    print(f"imported {count} modules, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
