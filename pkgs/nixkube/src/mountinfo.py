# SPDX-License-Identifier: MIT

"""Reading /proc/<pid>/mountinfo."""

import re

_ESCAPE = re.compile(r"\\([0-7]{3})")


def unescape(field: str) -> str:
    """A path field: the kernel writes space, tab, newline and backslash as
    octal escapes."""
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)
