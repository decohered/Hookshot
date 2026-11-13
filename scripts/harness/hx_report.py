"""hx report <id> [--root DIR]: one run, printed from its record and nothing else.

Every line comes from the run's manifest. Before anything is printed, each run file and screenshot the manifest names is
read back and checked against its recorded sha256 and size: one that changed or went missing refuses the whole report,
and the report names its path on stderr. A manifest of another shape refuses it too, naming the run and what is
wrong. The lines, in order:

    run <id>
    written <written>
    manifest <sha256 of manifest.json>
    prior <id> <sha256>            (or: prior none)
    facts
      <key>: <json of the value, sorted keys>        (one per fact, in record.FACT_KEYS order)
    files
      <sha256> <size> <path>                          (sorted by path)
    screenshots
      <sha256> <size> <name>                          (sorted by name)

--root defaults to the home folder's hookshot-harness/results.
"""
import argparse
import json
import sys
from pathlib import Path

import record


def default_root():
    return Path.home() / "hookshot-harness" / "results"


def _is_entry(entry):
    return isinstance(entry, dict) and isinstance(entry.get("sha256"), str) and isinstance(entry.get("size"), int)


def _is_prior(prior):
    return isinstance(prior, dict) and isinstance(prior.get("id"), str) and isinstance(prior.get("sha256"), str)


def _shape_problem(manifest):
    """Why a parsed manifest lacks what render reads (id, written, facts, prior, files, screenshots), or None"""
    if not isinstance(manifest, dict):
        return "its manifest is not a JSON object"
    for field in ("id", "written"):
        if field not in manifest:
            return f"its manifest has no {field}"
    facts = manifest.get("facts")
    if not isinstance(facts, dict):
        return "its manifest's facts are not an object"
    missing = [key for key in record.FACT_KEYS if key not in facts]
    if missing:
        return "its manifest's facts lack " + ", ".join(missing)
    if "prior" not in manifest or not (manifest["prior"] is None or _is_prior(manifest["prior"])):
        return "its manifest's prior is neither null nor a run id with a sha256"
    for field in ("files", "screenshots"):
        entries = manifest.get(field)
        if not isinstance(entries, dict) or not all(_is_entry(entry) for entry in entries.values()):
            return f"its manifest's {field} are not sha256 and size entries"
    return None


def _check(path, entry):
    """Raises RecordError unless the file at path holds the bytes its manifest entry recorded"""
    try:
        data = path.read_bytes()
    except (OSError, ValueError) as e:
        raise record.RecordError(f"{path} cannot be read: {e}") from e
    if record.sha256_bytes(data) != entry["sha256"] or len(data) != entry["size"]:
        raise record.RecordError(f"{path} does not match its record")


def render(root, run_id):
    """The report of one run as text; every file the manifest names is checked before the text is returned"""
    manifest, manifest_sha = record.read_manifest(root, run_id)
    problem = _shape_problem(manifest)
    if problem is not None:
        raise record.RecordError(f"run {run_id}: {problem}")
    folder = Path(root) / "runs" / run_id
    for name, entry in manifest["files"].items():
        _check(folder / "files" / name, entry)
    for entry in manifest["screenshots"].values():
        _check(Path(root) / "screenshots" / entry["sha256"], entry)

    lines = [f"run {manifest['id']}", f"written {manifest['written']}", f"manifest {manifest_sha}"]
    prior = manifest["prior"]
    lines.append("prior none" if prior is None else f"prior {prior['id']} {prior['sha256']}")
    lines.append("facts")
    for key in record.FACT_KEYS:
        lines.append(f"  {key}: {json.dumps(manifest['facts'][key], sort_keys=True)}")
    lines.append("files")
    for name in sorted(manifest["files"]):
        entry = manifest["files"][name]
        lines.append(f"  {entry['sha256']} {entry['size']} {name}")
    lines.append("screenshots")
    for name in sorted(manifest["screenshots"]):
        entry = manifest["screenshots"][name]
        lines.append(f"  {entry['sha256']} {entry['size']} {name}")
    return "\n".join(lines) + "\n"


def main(argv):
    parser = argparse.ArgumentParser(prog="hx report", description="Print one run from its record.")
    parser.add_argument("id", help="the run's six-digit id")
    parser.add_argument("--root", type=Path, default=None, help="the record root (default: ~/hookshot-harness/results)")
    args = parser.parse_args(argv)
    root = default_root() if args.root is None else args.root
    try:
        text = render(root, args.id)
    except record.RecordError as e:
        print(f"hx report: {e}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(text.encode("utf-8"))
    return 0
