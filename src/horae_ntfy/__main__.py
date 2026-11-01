# SPDX-License-Identifier: Apache-2.0
"""`python -m horae_ntfy` serves the plugin over stdio; `--manifest` prints `plugin.yaml`."""

import sys

from horae_sdk import SinkError, dump_manifest

from horae_ntfy.http import self_test
from horae_ntfy.plugin import build, manifest

HEADER = """\
# Written by `uv run python -m horae_ntfy --manifest > plugin.yaml`; tests/test_manifest.py
# checks it is what the SDK writes. When installing, pin runtime.command to an absolute
# interpreter that has this package (horae-sdk README, "The command").
"""


def main(argv: list[str]) -> None:
    if argv == ["--manifest"]:
        sys.stdout.write(HEADER + dump_manifest(manifest()))
        return
    if argv:
        sys.exit("usage: python -m horae_ntfy [--manifest]")
    try:
        self_test()  # fail at startup, not on the first push, if httpx2 changed
    except SinkError as error:
        sys.exit(f"horae_ntfy: {error.kind}: {error}")
    # Never print while serving: stdout is the protocol channel.
    build().run()


if __name__ == "__main__":
    main(sys.argv[1:])
