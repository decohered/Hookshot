"""hx mangohud CSV RUN [--root DIR]: a MangoHud log placed beside a recorded run, as a new record run that names it.

The new run holds mangohud.csv (the CSV's bytes) and mangohud.json (kind mangohud, the target run's id, and the CSV's sha256
and size), and carries the target run's facts. It is written beside the target, never inside its folder: a record run is
written once, and hx verify names every file a manifest does not name. hx report reads it like any run.

RUN must be a six-digit id whose manifest reads back, and CSV must be a readable, non-empty file. A refusal names its cause
on stderr, writes nothing and exits 2. On success the new run's id is printed as `run: <id>`.

--root defaults to the home folder's hookshot-harness/results.
"""
import argparse
import sys
from pathlib import Path

import hx_report
import hx_run
import record


def place(root, csv_path, run_id):
    """The record run that holds the CSV beside run_id, with run_id's facts: returns the new run's id"""
    manifest, _ = record.read_manifest(root, run_id)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("facts"), dict):
        raise record.RecordError(f"run {run_id}: its manifest has no facts object")
    try:
        data = Path(csv_path).read_bytes()
    except OSError as e:
        raise record.RecordError(f"{csv_path} cannot be read: {e.strerror or e}") from e
    if not data:
        raise record.RecordError(f"{csv_path} is empty")
    summary = {"kind": "mangohud", "target": run_id, "csv": {"sha256": record.sha256_bytes(data), "size": len(data)}}
    files = {"mangohud.csv": data, "mangohud.json": hx_run.dump(summary)}
    new_id, _ = record.write_run(root, manifest["facts"], files, {})
    return new_id


def main(argv):
    parser = argparse.ArgumentParser(prog="hx mangohud", description="Place a MangoHud CSV beside a recorded run.")
    parser.add_argument("csv", type=Path, help="the MangoHud log (a CSV file)")
    parser.add_argument("run", help="the six-digit id of the run the log belongs to")
    parser.add_argument("--root", type=Path, default=None, help="the record root (default: ~/hookshot-harness/results)")
    args = parser.parse_args(argv)
    root = hx_report.default_root() if args.root is None else args.root
    try:
        new_id = place(root, args.csv, args.run)
    except record.RecordError as e:
        print(f"hx mangohud: {e}", file=sys.stderr)
        return 2
    print(f"run: {new_id}")
    return 0
