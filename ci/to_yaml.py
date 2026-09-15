"""Render a workflow's JSON as YAML, with a literal block for every multi-line string.

`pkgs.formats.yaml` is remarshal, which writes a multi-line string as one
escaped double-quoted scalar. A `run:` body holding a ten-line shell script
then arrives as a single 600-column line with `\n` in it, which nobody reads
and no reviewer can diff.

PyYAML's resolvers are YAML 1.1's, which is what quotes the `on:` key. A
workflow needs that: to a 1.1 parser an unquoted `on` is the boolean `true`.
"""

from __future__ import annotations

import json
import sys

import yaml


class BlockDumper(yaml.SafeDumper):
    """A dumper whose only change is the string style below."""


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.Node:
    # PyYAML falls back to a quoted scalar on its own where a literal block
    # cannot hold the string -- a line ending in whitespace, for one.
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


BlockDumper.add_representer(str, _represent_str)


def main() -> None:
    with open(sys.argv[1], encoding="utf-8") as handle:
        value = json.load(handle)
    sys.stdout.write(
        yaml.dump(
            value,
            Dumper=BlockDumper,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=True,
            # No wrapping. A substituter list is one long value, and a line
            # broken in the middle of it is harder to read than a long one.
            width=10**6,
        )
    )


if __name__ == "__main__":
    main()
