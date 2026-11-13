"""hx verify [--root DIR] [--journal FILE]: the whole record checked against its hashes, its chain and the journal, with
every problem named. Verify only reads: nothing under the root is written, moved or removed.

Each run's manifest is read through record.py. Every run file and screenshot the manifest names is read back and checked
against its recorded sha256 and size, and the run folder is checked for files its manifest does not name. A run's prior
must name the run before it and that run's manifest hash, and the first run has no prior. The journal must anchor every
run in the record with its manifest hash, and must not name a run the record lacks.

--root defaults to the home folder's hookshot-harness/results. --journal FILE reads a file of
`journalctl --user -t hookshot-harness -o cat` output; without it, the user's journal is read.

Output: one line per problem, each beginning with the path or run it names, and exit 1. With none, one line counting the
runs checked and exit 0. A root that is not a folder, or a journal that cannot be read, exits 2 with the reason on
stderr.
"""
import argparse
import sys
from pathlib import Path

import hx_report
import record


def check(root, journal):
    """Every problem in the record against the journal's (run id, manifest sha256) pairs, as lines, and the number of
    runs checked"""
    ids = record.run_ids(root)
    anchors = {}
    for run_id, digest in journal:
        anchors.setdefault(run_id, []).append(digest)
    problems = []
    previous = None  # (run id, manifest sha256 or None when the manifest is unreadable) of the run before this one
    for run_id in ids:
        try:
            manifest, digest = record.read_manifest(root, run_id)
        except record.RecordError as e:
            problems.append(str(e))
            digest = None
        else:
            shape = _shape_problem(manifest)
            if shape:
                problems.append(f"run {run_id}: {shape}")
            else:
                problems.extend(_folder_problems(root, run_id, manifest))
                problems.extend(_chain_problems(run_id, manifest, previous))
        if not anchors.get(run_id):
            problems.append(f"run {run_id}: not in the journal")
        elif digest is not None:
            for anchored in anchors[run_id]:
                if anchored != digest:
                    problems.append(f"run {run_id}: the journal anchors manifest {anchored}, "
                                    f"but its manifest hashes to {digest}")
        previous = (run_id, digest)
    for run_id in sorted(set(anchors) - set(ids)):
        problems.append(f"run {run_id}: in the journal, not in the record")
    return problems, len(ids)


def _is_entry(entry):
    return isinstance(entry, dict) and isinstance(entry.get("sha256"), str) and isinstance(entry.get("size"), int)


def _is_prior(prior):
    return isinstance(prior, dict) and isinstance(prior.get("id"), str) and isinstance(prior.get("sha256"), str)


def _shape_problem(manifest):
    """Why a parsed manifest lacks the fields verify reads (prior, files, screenshots), or None"""
    if not isinstance(manifest, dict):
        return "its manifest is not a JSON object"
    if "prior" not in manifest or not (manifest["prior"] is None or _is_prior(manifest["prior"])):
        return "its manifest's prior is neither null nor a run id with a sha256"
    for field in ("files", "screenshots"):
        entries = manifest.get(field)
        if not isinstance(entries, dict) or not all(_is_entry(entry) for entry in entries.values()):
            return f"its manifest's {field} are not sha256 and size entries"
    return None


def _chain_problems(run_id, manifest, previous):
    """The run's prior against the run before it: the first run has none, and each later run names that run and its
    hash"""
    prior = manifest["prior"]
    if previous is None:
        if prior is None:
            return []
        return [f"run {run_id}: the first run in the record has a prior, naming run {prior['id']}"]
    prev_id, prev_sha = previous
    if prior is None:
        return [f"run {run_id}: has no prior, but run {prev_id} comes before it"]
    if prior["id"] != prev_id:
        return [f"run {run_id}: prior names run {prior['id']}, but the run before it is run {prev_id}"]
    if prev_sha is None:
        return [f"run {run_id}: prior {prev_id} cannot be checked, run {prev_id}'s manifest is unreadable"]
    if prior["sha256"] != prev_sha:
        return [f"run {run_id}: prior names manifest {prior['sha256']}, "
                f"but run {prev_id}'s manifest hashes to {prev_sha}"]
    return []


def _mismatch(path, entry):
    """Why the file at path is not the bytes its manifest entry records, or None when it is"""
    try:
        data = path.read_bytes()
    except OSError as e:
        return f"cannot be read: {e.strerror or e}"
    if record.sha256_bytes(data) != entry["sha256"] or len(data) != entry["size"]:
        return "does not match its manifest"
    return None


def _folder_problems(root, run_id, manifest):
    """A run's files and screenshots against its manifest: each one missing or changed, and each file in the run folder
    that the manifest does not name"""
    folder = Path(root) / "runs" / run_id
    named = {"manifest.json"} | {f"files/{name}" for name in manifest["files"]}
    present = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
    problems = []
    for name, entry in manifest["files"].items():
        shown = f"runs/{run_id}/files/{name}"
        if f"files/{name}" not in present:
            problems.append(f"{shown}: missing, its manifest names it")
            continue
        reason = _mismatch(folder / "files" / name, entry)
        if reason is not None:
            problems.append(f"{shown}: {reason}")
    for rel in sorted(present - named):
        problems.append(f"runs/{run_id}/{rel}: added, its manifest does not name it")

    shots = Path(root) / "screenshots"
    stored = {p.name: p for p in shots.iterdir() if p.is_file()} if shots.is_dir() else {}
    for name, entry in manifest["screenshots"].items():
        shown = f"screenshots/{entry['sha256']}"
        if entry["sha256"] not in stored:
            problems.append(f"{shown}: missing, run {run_id}'s screenshot {name} needs it")
            continue
        reason = _mismatch(stored[entry["sha256"]], entry)
        if reason is not None:
            problems.append(f"{shown}: {reason} (run {run_id}, screenshot {name})")
    return problems


def main(argv):
    parser = argparse.ArgumentParser(prog="hx verify",
                                     description="Check the whole record against its hashes, its chain and the "
                                                 "journal.")
    parser.add_argument("--root", type=Path, default=None, help="the record root (default: ~/hookshot-harness/results)")
    parser.add_argument("--journal", type=Path, default=None,
                        help="a file of journalctl output for the harness's journal, read instead of the "
                             "user's journal")
    args = parser.parse_args(argv)
    root = hx_report.default_root() if args.root is None else args.root
    if not root.is_dir():
        print(f"hx verify: no record root at {root}", file=sys.stderr)
        return 2
    try:
        if args.journal is None:
            text = record.read_journal()
        else:
            text = args.journal.read_text(encoding="utf-8", errors="replace")
    except (OSError, record.RecordError) as e:
        source = "the user's journal" if args.journal is None else str(args.journal)
        print(f"hx verify: cannot read {source}: {e}", file=sys.stderr)
        return 2
    problems, count = check(root, record.parse_journal(text))
    if problems:
        print("\n".join(problems))
        return 1
    print(f"{count} run{'s' * (count != 1)} checked, no problems")
    return 0
