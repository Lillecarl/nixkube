# SPDX-License-Identifier: MIT
"""Seeds a node's Nix store and starts the application it holds.

**This package imports nothing outside the standard library, and that is a
requirement rather than a preference.** It runs in the position where the
wanted version could not be fetched, so any third-party import it makes is one
more thing that can keep a node from starting at all -- and the closure it
would add ships in the container image, which is the thing being kept small.

README.md says what the two modes do and why the image carries a second copy
of each application.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
