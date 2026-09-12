"""Command-line entrypoint.

Only `--version` exists so far. Subcommands arrive with the packages they drive:
`run` and `apply` in the pipeline phase, `login` / `labels` / `config` alongside
the Gmail and settings work.
"""

import argparse
from collections.abc import Sequence

from epc import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="epc",
        description="Classify Gmail threads by how urgently they need attention.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
