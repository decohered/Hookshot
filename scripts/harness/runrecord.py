"""One run of the harness's record, written from a job. facts() turns a job's build (the build.txt that hx build writes, its
dirty diff and its sha256sum output), its environment and its machine's command outputs into the facts record.py
requires; every text in them is scrubbed of the home path and the user name, and the build line is not passed.
machine_outputs() runs the machine's commands, and write() writes the run under the record root's results folder and
returns its id and manifest hash. A fact that cannot be gathered is a gap; a refusal from record.py is raised, not caught.

Stdlib only. The commands import this module as a sibling; tests/test_harness_runrecord.py checks it.
"""
import subprocess
from pathlib import Path

import record


class RunRecordError(Exception):
    """A build's record that cannot be read: a line that is not 'key: value', or no sha or closure line"""


def results_dir(root):
    """The record root under a harness root: the folder that hx report and hx verify read by default"""
    return Path(root) / "results"


def machine_outputs(run=subprocess.run):
    """Each of record.MACHINE_COMMANDS run once, by name: its stdout as text, or None when it exits nonzero or is
    missing. No command is retried."""
    outputs = {}
    for name, argv in record.MACHINE_COMMANDS.items():
        try:
            done = run(argv, capture_output=True)
        except OSError:
            outputs[name] = None
        else:
            outputs[name] = done.stdout.decode("utf-8", "replace") if done.returncode == 0 else None
    return outputs


def gpu_index(vulkaninfo_text, expect):
    """The index of the GPU<n> block of a vulkaninfo --summary text whose deviceName is expect, or None when no block
    lists it, or more than one does: two GPUs of one name cannot be told apart by name."""
    if vulkaninfo_text is None:
        return None
    matches, index = [], None
    for line in vulkaninfo_text.splitlines():
        header = record.VULKAN_GPU.fullmatch(line)
        field = record.VULKAN_FIELD.fullmatch(line)
        if header:
            index = int(header.group(1))
        elif field and index is not None:
            if field.group(1) == "deviceName" and field.group(2).strip() == expect:
                matches.append(index)
        else:
            index = None
    return matches[0] if len(matches) == 1 else None


def scrub(text, home, user):
    """text with the home path replaced by ~ and the user name by user; an empty home or user names nothing to replace"""
    if home:
        text = text.replace(home, "~")
    if user:
        text = text.replace(user, "user")
    return text


def _scrubbed(value, home, user):
    """value with every text in it, keys and strings at any depth, scrubbed"""
    if isinstance(value, str):
        return scrub(value, home, user)
    if isinstance(value, dict):
        return {_scrubbed(key, home, user): _scrubbed(inner, home, user) for key, inner in value.items()}
    return value


def _build_fields(build_txt):
    """build.txt's 'key: value' lines by key; its sha and closure lines are required"""
    fields = {}
    for number, line in enumerate(build_txt.splitlines(), 1):
        key, sep, value = line.partition(": ")
        if not sep:
            raise RunRecordError(f"build.txt line {number} is not 'key: value'")
        fields[key] = value
    for key in ("sha", "closure"):
        if key not in fields:
            raise RunRecordError(f"build.txt has no {key} line")
    return fields


def facts(build_txt, diff, sha256sum, env, expect, outputs, home, user):
    """A run's facts, every text in them scrubbed of home and user. build_txt is the build's build.txt, diff its
    dirty.diff ("" when the checkout was clean), sha256sum the sha256sum output for the job's binaries, env the job's
    environment, expect the deviceName of the GPU the job ran on, and outputs what machine_outputs returned. A value
    that cannot be gathered is a gap; a refusal from record.py is raised."""
    build = _build_fields(build_txt)
    built = record.build_facts({"hookshot": build["sha"]}, diff, sha256sum, build["closure"], env)
    machine = record.machine_facts(gpu_index(outputs.get("vulkaninfo"), expect), outputs)
    return _scrubbed({**built, **machine}, home, user)


def write(root, facts, files, journal=record.write_journal):
    """Writes one run under results_dir(root) and anchors it in the journal; returns its id and its manifest's sha256.
    The run keeps no screenshots. A refusal from record.write_run is raised."""
    return record.write_run(results_dir(root), facts, files, {}, journal)
