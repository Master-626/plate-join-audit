"""CLI defaults to stdout; optional report output never overwrites a path."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from . import AuditError, __version__, audit


def _save(path, text):
    target = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=target.absolute().parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Audit plate table keys; never join or edit inputs."
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("layout")
    parser.add_argument("readings")
    parser.add_argument("--plate-size", type=int, default=96)
    parser.add_argument("--layout-plate", default="plate")
    parser.add_argument("--layout-well", default="well")
    parser.add_argument("--readings-plate", default="plate")
    parser.add_argument("--readings-well", default="well")
    parser.add_argument("--observation-column", action="append", default=[])
    parser.add_argument("--layout-tsv", action="store_true")
    parser.add_argument("--readings-tsv", action="store_true")
    parser.add_argument("--output", help="new JSON path; existing files/links are never replaced")
    args = parser.parse_args(argv)
    try:
        report = audit(
            args.layout,
            args.readings,
            plate_size=args.plate_size,
            layout_plate=args.layout_plate,
            layout_well=args.layout_well,
            readings_plate=args.readings_plate,
            readings_well=args.readings_well,
            observation_columns=tuple(args.observation_column),
            layout_delimiter="\t" if args.layout_tsv else ",",
            readings_delimiter="\t" if args.readings_tsv else ",",
        )
        output = json.dumps(report, ensure_ascii=True, indent=2) + "\n"
        if args.output:
            _save(args.output, output)
        else:
            sys.stdout.write(output)
        return 0 if report["ok"] else 1
    except (AuditError, OSError) as exc:
        print(f"plate-join-audit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
