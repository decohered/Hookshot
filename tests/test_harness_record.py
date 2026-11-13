"""scripts/harness/record.py, the harness's record of runs, against temporary roots and an injected journal: a run's
folder is written once with its files, its screenshots by hash and the prior manifest's hash; a refused write changes
nothing; the journal line anchors a run; runs read back. The facts gatherers parse fixture command outputs and run no
command. hx verify runs as a subprocess on fixture runs and a fixture journal file: a clean record exits 0, each edit,
break or gap is named and exits 1, and an unreadable journal or a missing root exits 2. No real journal, GPU, game or
network is used.

python -I -B tests/test_harness_record.py
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HX = ROOT / "scripts" / "harness" / "hx.py"
sys.path.insert(0, str(ROOT / "scripts" / "harness"))
import record


def facts(**changes):
    """A facts dict with every FACT_KEYS key; values are stored verbatim, so nested values are used too"""
    f = {key: {"value": key} for key in record.FACT_KEYS}
    f.update(changes)
    return f


def sha(data):
    return record.sha256_bytes(data)


class RecordTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "record"
        self.messages = []

    def write(self, files=None, shots=None, fact=None, journal=None):
        """write_run into the test's root, with the test's journal unless one is given"""
        return record.write_run(self.root, facts() if fact is None else fact, files or {}, shots or {},
                                journal=journal or self.messages.append)

    def snapshot(self):
        """Every path under the root with the bytes of each file: what a refused write must leave alone"""
        if not self.root.exists():
            return {}
        return {p.relative_to(self.root).as_posix(): p.read_bytes() if p.is_file() else None
                for p in sorted(self.root.rglob("*"))}

    def test_runs_chain_through_the_prior_manifest_hash(self):
        first_id, first_sha = self.write(files={"a.txt": b"one"})
        second_id, second_sha = self.write(files={"a.txt": b"two"})
        self.assertEqual((first_id, second_id), ("000001", "000002"))

        first_bytes = (self.root / "runs" / "000001" / "manifest.json").read_bytes()
        second_bytes = (self.root / "runs" / "000002" / "manifest.json").read_bytes()
        self.assertEqual(sha(first_bytes), first_sha)
        self.assertEqual(sha(second_bytes), second_sha)

        first = json.loads(first_bytes)
        second = json.loads(second_bytes)
        self.assertEqual(first_bytes, (json.dumps(first, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        self.assertEqual(second_bytes, (json.dumps(second, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        self.assertEqual(sorted(first), ["facts", "files", "format", "id", "prior", "screenshots", "written"])
        self.assertEqual((first["format"], first["id"]), (1, "000001"))
        self.assertIsNone(first["prior"])
        self.assertEqual(second["prior"], {"id": "000001", "sha256": first_sha})
        self.assertEqual(first["facts"], facts())
        self.assertEqual(datetime.datetime.fromisoformat(first["written"]).utcoffset(), datetime.timedelta(0))
        self.assertEqual(record.read_manifest(self.root, "000002"), (second, second_sha))

    def test_files_and_screenshots_are_stored_by_hash(self):
        data = bytes(range(256)) * 3
        shot = b"\x89PNG screenshot bytes"
        run_id, _ = self.write(files={"logs/out.bin": data, "note.txt": b"hi"},
                               shots={"shot-1.png": shot, "shot-2.png": shot})
        folder = self.root / "runs" / run_id
        self.assertEqual((folder / "files" / "logs" / "out.bin").read_bytes(), data)
        self.assertEqual((folder / "files" / "note.txt").read_bytes(), b"hi")

        manifest, _ = record.read_manifest(self.root, run_id)
        self.assertEqual(manifest["files"], {"logs/out.bin": {"sha256": sha(data), "size": len(data)},
                                             "note.txt": {"sha256": sha(b"hi"), "size": 2}})
        self.assertEqual(manifest["screenshots"], {"shot-1.png": {"sha256": sha(shot), "size": len(shot)},
                                                   "shot-2.png": {"sha256": sha(shot), "size": len(shot)}})

        self.assertEqual((self.root / "screenshots" / sha(shot)).read_bytes(), shot)
        self.write(shots={"again.png": shot})
        self.assertEqual([p.name for p in (self.root / "screenshots").iterdir()], [sha(shot)])
        self.assertFalse([p for p in folder.rglob("*") if p.is_file() and p.read_bytes() == shot])

    def test_a_stored_screenshot_with_other_bytes_is_refused(self):
        shot = b"real screenshot"
        blob = self.root / "screenshots" / sha(shot)
        blob.parent.mkdir(parents=True)
        blob.write_bytes(b"damaged")
        with self.assertRaises(record.RecordError) as caught:
            self.write(shots={"s.png": shot})
        self.assertIn(sha(shot), str(caught.exception))
        self.assertEqual(blob.read_bytes(), b"damaged")
        self.assertEqual(sorted(p.name for p in (self.root / "runs").iterdir()), [])
        self.assertEqual(self.messages, [])

    def test_refused_writes_change_nothing(self):
        self.write(files={"keep.txt": b"keep"}, shots={"keep.png": b"keep"})
        before = self.snapshot()
        missing = facts()
        del missing["load"]
        cases = {
            "a missing fact": (missing, {"a.txt": b"x"}),
            "an absolute path": (facts(), {"/etc/a.txt": b"x"}),
            "an empty path": (facts(), {"": b"x"}),
            "a '..' path": (facts(), {"../escape.txt": b"x"}),
            "a '..' inside a path": (facts(), {"a/../../escape.txt": b"x"}),
            "a '.' path": (facts(), {".": b"x"}),
            "a backslash path": (facts(), {"a\\..\\escape.txt": b"x"}),
            "two paths for one file": (facts(), {"a//b.txt": b"x", "a/b.txt": b"y"}),
            "facts that are not JSON": (facts(gpu={"speed": {1, 2}}), {"a.txt": b"x"}),
        }
        for label, (fact, files) in cases.items():
            with self.subTest(label), self.assertRaises(record.RecordError):
                self.write(files=files, shots={"new.png": b"new"}, fact=fact)
            self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.base / "escape.txt").exists())
        self.assertEqual(len(self.messages), 1)

    def test_a_refusal_on_a_new_root_creates_nothing(self):
        with self.assertRaises(record.RecordError):
            self.write(files={"a.txt": b"x"}, fact=facts(gpu={"speed": {1, 2}}))
        self.assertFalse(self.root.exists())
        self.assertEqual(self.messages, [])

    def test_an_id_taken_by_a_racing_writer_is_refused(self):
        self.write(files={"a.txt": b"first"})
        manifest_before = (self.root / "runs" / "000001" / "manifest.json").read_bytes()
        # The writer's listing predates the racer's run 000001, so the writer aims at 000001 too.
        with mock.patch.object(record, "run_ids", return_value=[]):
            with self.assertRaises(record.RecordError) as caught:
                self.write(files={"b.txt": b"second"}, shots={"s.png": b"shot"})
        self.assertIn("000001", str(caught.exception))
        self.assertEqual(record.run_ids(self.root), ["000001"])
        self.assertEqual(sorted(p.name for p in (self.root / "runs").iterdir()), ["000001"])
        self.assertEqual((self.root / "runs" / "000001" / "manifest.json").read_bytes(), manifest_before)
        self.assertEqual(len(self.messages), 1)

    def test_the_journal_gets_the_line_once_after_the_folder_is_complete(self):
        seen = []

        def journal(message):
            folder = self.root / "runs" / "000001"
            seen.append((message, (folder / "manifest.json").is_file() and (folder / "files" / "a.txt").is_file()))
        run_id, digest = self.write(files={"a.txt": b"a"}, journal=journal)
        self.assertEqual(seen, [(record.journal_line(run_id, digest), True)])

    def test_a_failing_journal_is_a_record_error_naming_the_run(self):
        def journal(message):
            raise RuntimeError("no journal here")

        with self.assertRaises(record.RecordError) as caught:
            self.write(files={"a.txt": b"a"}, journal=journal)
        self.assertIn("000001", str(caught.exception))
        self.assertEqual(record.run_ids(self.root), ["000001"])

    def test_write_journal_pipes_the_message_to_systemd_cat(self):
        calls = []
        message = "run=000001 manifest=" + "0" * 64

        def fake(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        record.write_journal(message, run=fake)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], ["systemd-cat", "-t", "hookshot-harness"])
        self.assertEqual(calls[0][1]["input"], message.encode("utf-8"))

    def test_read_journal_runs_journalctl_for_this_tag(self):
        calls = []
        line = record.journal_line("000001", sha(b"one"))

        def fake(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, (line + "\n").encode("utf-8"), b"")

        self.assertEqual(record.read_journal(run=fake), line + "\n")
        self.assertEqual(calls, [["journalctl", "--user", "-t", "hookshot-harness", "-o", "cat", "--no-pager"]])

    def test_a_journal_tool_that_fails_or_is_missing_is_a_record_error(self):
        def exits(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, b"", b"no user bus")

        def missing(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", argv[0])

        for name, fake in (("exit 1", exits), ("missing", missing)):
            with self.subTest(name, tool="write_journal"), self.assertRaises(record.RecordError):
                record.write_journal("x", run=fake)
            with self.subTest(name, tool="read_journal"), self.assertRaises(record.RecordError):
                record.read_journal(run=fake)

    def test_parse_journal_reverses_journal_line_and_skips_other_lines(self):
        one, two = sha(b"one"), sha(b"two")
        self.assertEqual(record.journal_line("000001", one), "run=000001 manifest=" + one)
        text = "\n".join([
            "systemd: started something",
            record.journal_line("000001", one),
            "run=12 manifest=" + one,
            "run=000002 manifest=ABC",
            record.journal_line("000002", two),
            "",
        ])
        self.assertEqual(record.parse_journal(text), [("000001", one), ("000002", two)])
        self.assertEqual(record.parse_journal(record.journal_line("000003", two)), [("000003", two)])

    def test_run_ids_ignores_entries_that_are_not_runs(self):
        self.assertEqual(record.run_ids(self.root), [])
        self.write(files={"a.txt": b"1"})
        self.write(files={"a.txt": b"2"})
        runs = self.root / "runs"
        (runs / "00003").mkdir()
        (runs / "notes").mkdir()
        (runs / "000007").write_bytes(b"")
        (runs / ".staging-x").mkdir()
        self.assertEqual(record.run_ids(self.root), ["000001", "000002"])

    def test_read_manifest_refuses_a_name_that_is_not_a_run_id(self):
        self.write(files={"a.txt": b"1"})
        for name in ("../runs/000001", "1", "000001/..", "000001 "):
            with self.subTest(name), self.assertRaises(record.RecordError):
                record.read_manifest(self.root, name)
        with self.assertRaises(record.RecordError):
            record.read_manifest(self.root, "000009")


# Command outputs as a machine prints them: an NVIDIA GPU 0 beside an Intel GPU 1 with Mesa. The identifiers are made up.
VULKANINFO = "\n".join([
    "==========",
    "VULKANINFO",
    "==========",
    "",
    "Vulkan Instance Version: 1.3.275",
    "",
    "",
    "Instance Extensions: count = 2",
    "------------------------------",
    "VK_KHR_surface                         : extension revision 25",
    "VK_KHR_xcb_surface                     : extension revision 6",
    "",
    "",
    "Devices:",
    "========",
    "GPU0:",
    "\tapiVersion         = 1.3.277",
    "\tdriverVersion      = 550.144.3",
    "\tvendorID           = 0x10de",
    "\tdeviceID           = 0x2206",
    "\tdeviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU",
    "\tdeviceName         = NVIDIA GeForce RTX 3080",
    "\tdriverID           = DRIVER_ID_NVIDIA_PROPRIETARY",
    "\tdriverName         = NVIDIA",
    "\tdriverInfo         = 550.144.03",
    "\tconformanceVersion = 1.3.8.0",
    "\tdeviceUUID         = 5a1f0c2e-3b7d-4e8a-9c61-0d2f7b8e4a10",
    "\tdriverUUID         = 0f6c2d1e-8a4b-4c3d-9e2f-7a1b5c6d8e90",
    "GPU1:",
    "\tapiVersion         = 1.3.280",
    "\tdriverVersion      = 24.2.8",
    "\tvendorID           = 0x8086",
    "\tdeviceID           = 0xa780",
    "\tdeviceType         = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU",
    "\tdeviceName         = Intel(R) UHD Graphics 770 (RPL-S)",
    "\tdriverID           = DRIVER_ID_INTEL_OPEN_SOURCE_MESA",
    "\tdriverName         = Intel open-source Mesa driver",
    "\tdriverInfo         = Mesa 24.2.8",
    "\tconformanceVersion = 1.3.8.2",
    "\tdeviceUUID         = 2c9e7b41-65d0-4f1a-b8e3-93a0d4c7f512",
    "\tdriverUUID         = 7d3a1f0b-2e5c-4a8d-91b6-c4e0f8a2d713",
    "",
])
NVIDIA_SMI = "GPU-5a1f0c2e-3b7d-4e8a-9c61-0d2f7b8e4a10, NVIDIA GeForce RTX 3080, 550.144.03, 1830, 1830, 9501\n"
KERNEL = "Linux version 6.8.0-45-generic (builder@example.invalid) (gcc (GCC) 13.2.0, GNU ld 2.42) #47-generic SMP\n"
LOAD = "0.52 0.58 0.59 1/1234 5678\n"
CPU_CLOCKS = "".join(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq:{khz}\n"
                     for i, khz in enumerate((3600000, 3000000, 1400000, 1400000)))
NVIDIA_BLOCK = {
    "apiVersion": "1.3.277", "driverVersion": "550.144.3", "vendorID": "0x10de", "deviceID": "0x2206",
    "deviceType": "PHYSICAL_DEVICE_TYPE_DISCRETE_GPU", "deviceName": "NVIDIA GeForce RTX 3080",
    "driverID": "DRIVER_ID_NVIDIA_PROPRIETARY", "driverName": "NVIDIA", "driverInfo": "550.144.03",
    "conformanceVersion": "1.3.8.0", "deviceUUID": "5a1f0c2e-3b7d-4e8a-9c61-0d2f7b8e4a10",
    "driverUUID": "0f6c2d1e-8a4b-4c3d-9e2f-7a1b5c6d8e90",
}
CPU_KHZ = {"cpu0": 3600000, "cpu1": 3000000, "cpu2": 1400000, "cpu3": 1400000}
SHAS = {"hookshot": "3f9c2a1b7d4e6f80a1b2c3d4e5f60718293a4b5c", "devenv": "9a8b7c6d5e4f30211a0b9c8d7e6f5a4b3c2d1e0f"}
DIFF = ("diff --git a/scripts/harness/record.py b/scripts/harness/record.py\n"
        "--- a/scripts/harness/record.py\n+++ b/scripts/harness/record.py\n"
        "@@ -1 +1,2 @@\n docstring\n+FACT_KEYS = ()\n")
SHA256SUM = f"{sha(b'exe')}  build/hookshot\n{sha(b'lib')} *build/libhookshot.so\n"
CLOSURE = "/nix/store/q4kx1h7v3d2m9b6n8c5p0r1s2t3u4w5y-hookshot-1.0"
ENV = {"RX_VK_DEVICE": "0", "DEVENV_PROFILE": "default"}


def outputs(**changes):
    """A machine's command outputs by MACHINE_COMMANDS name; a change sets a name to its text or to None (failed)"""
    found = {"vulkaninfo": VULKANINFO, "nvidia_smi": NVIDIA_SMI, "kernel": KERNEL, "load": LOAD,
             "cpu_clocks": CPU_CLOCKS}
    found.update(changes)
    return found


def build_args(**changes):
    """build_facts' arguments for a run that built two binaries; a change replaces one of them"""
    args = {"shas": SHAS, "diff": DIFF, "sha256sum": SHA256SUM, "closure": CLOSURE, "env": ENV}
    args.update(changes)
    return args


def edit(text, old, new=""):
    """The text with old replaced by new; old must be in it, or the case would test nothing"""
    if old not in text:
        raise AssertionError(f"fixture has no {old!r}")
    return text.replace(old, new)


def gaps(value, path=()):
    """Each gap in a facts value, by dotted path, with its reason: a dict whose only key is 'gap' is a gap"""
    if not isinstance(value, dict):
        return {}
    if set(value) == {"gap"}:
        return {".".join(path): value["gap"]}
    found = {}
    for key, inner in value.items():
        found.update(gaps(inner, path + (key,)))
    return found


class FactsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def test_machine_commands_are_the_five_the_gatherers_read(self):
        self.assertEqual(set(record.MACHINE_COMMANDS), {"nvidia_smi", "vulkaninfo", "kernel", "load", "cpu_clocks"})
        self.assertEqual(record.MACHINE_COMMANDS["nvidia_smi"][:2],
                         ["nvidia-smi", "--query-gpu=uuid,name,driver_version,clocks.gr,clocks.sm,clocks.mem"])
        self.assertEqual(record.MACHINE_COMMANDS["vulkaninfo"], ["vulkaninfo", "--summary"])

    def test_nvidia_gpu_beside_a_mesa_device(self):
        facts = record.machine_facts(0, outputs())
        self.assertEqual(facts["gpu"], NVIDIA_BLOCK)
        self.assertEqual(facts["driver"], {
            "vulkan": {"driverID": "DRIVER_ID_NVIDIA_PROPRIETARY", "driverName": "NVIDIA",
                       "driverInfo": "550.144.03", "driverVersion": "550.144.3"},
            "nvidia_smi": "550.144.03",
        })
        self.assertEqual(facts["mesa"], "24.2.8")
        self.assertEqual(facts["kernel"], "6.8.0-45-generic")
        self.assertEqual(facts["load"], {"1min": 0.52, "5min": 0.58, "15min": 0.59})
        self.assertEqual(facts["clocks"], {"gpu": {"graphics_mhz": 1830, "sm_mhz": 1830, "memory_mhz": 9501},
                                           "cpu_khz": CPU_KHZ})
        self.assertEqual(gaps(facts), {})

    def test_mesa_gpu_without_nvidia_smi_output_gaps_only_the_gpu_clocks(self):
        facts = record.machine_facts(1, outputs(nvidia_smi=None))
        self.assertEqual(facts["gpu"]["deviceName"], "Intel(R) UHD Graphics 770 (RPL-S)")
        self.assertEqual(facts["driver"], {"vulkan": {
            "driverID": "DRIVER_ID_INTEL_OPEN_SOURCE_MESA", "driverName": "Intel open-source Mesa driver",
            "driverInfo": "Mesa 24.2.8", "driverVersion": "24.2.8"}})
        self.assertEqual(facts["mesa"], "24.2.8")
        self.assertEqual(facts["clocks"]["cpu_khz"], CPU_KHZ)
        self.assertEqual(set(gaps(facts)), {"clocks.gpu"})
        self.assertTrue(gaps(facts)["clocks.gpu"].startswith("nvidia_smi:"))

    def test_mesa_gpu_has_no_nvidia_row_when_nvidia_smi_lists_another_gpu(self):
        facts = record.machine_facts(1, outputs())
        self.assertNotIn("nvidia_smi", facts["driver"])
        self.assertEqual(set(gaps(facts)), {"clocks.gpu"})

    def test_each_missing_or_unparsable_output_gaps_exactly_the_keys_it_feeds(self):
        no_nvidia = {"driver.nvidia_smi", "clocks.gpu"}
        cases = {
            "no vulkaninfo output": (outputs(vulkaninfo=None), 0, {"gpu", "driver", "mesa", "clocks.gpu"}),
            "vulkaninfo lists no GPU": (outputs(vulkaninfo="Devices:\n========\n"), 0,
                                        {"gpu", "driver", "mesa", "clocks.gpu"}),
            "chosen GPU not listed": (outputs(), 2, {"gpu", "driver", "clocks.gpu"}),
            "no nvidia-smi output": (outputs(nvidia_smi=None), 0, no_nvidia),
            "nvidia-smi row with five fields": (outputs(nvidia_smi=edit(NVIDIA_SMI, ", 9501")), 0, no_nvidia),
            "nvidia-smi row for another GPU": (outputs(nvidia_smi=edit(NVIDIA_SMI, "5a1f0c2e", "00000000")), 0,
                                               no_nvidia),
            "nvidia-smi clock not a number": (outputs(nvidia_smi=edit(NVIDIA_SMI, "9501", "[N/A]")), 0,
                                              {"clocks.gpu.memory_mhz"}),
            "chosen GPU lists no deviceUUID": (outputs(vulkaninfo=edit(
                VULKANINFO, "\tdeviceUUID         = 5a1f0c2e-3b7d-4e8a-9c61-0d2f7b8e4a10\n")), 0, no_nvidia),
            "chosen GPU lists no driverInfo": (outputs(vulkaninfo=edit(
                VULKANINFO, "\tdriverInfo         = 550.144.03\n")), 0, {"driver.vulkan.driverInfo"}),
            "no Mesa driver listed": (outputs(vulkaninfo=edit(VULKANINFO, "Mesa 24.2.8", "Intel 24.2.8")), 0,
                                      {"mesa"}),
            "no cpu_clocks output": (outputs(cpu_clocks=None), 0, {"clocks.cpu_khz"}),
            "cpu_clocks with no CPU": (outputs(cpu_clocks=""), 0, {"clocks.cpu_khz"}),
            "cpu_clocks line that is not a frequency": (outputs(cpu_clocks=edit(CPU_CLOCKS, ":3600000", ":fast")),
                                                        0, {"clocks.cpu_khz"}),
            "no kernel output": (outputs(kernel=None), 0, {"kernel"}),
            "kernel text with no release": (outputs(kernel="Linux\n"), 0, {"kernel"}),
            "no load output": (outputs(load=None), 0, {"load"}),
            "load with two averages": (outputs(load="0.52 0.58\n"), 0, {"load"}),
        }
        for label, (machine, chosen, expected) in cases.items():
            with self.subTest(label):
                facts = record.machine_facts(chosen, machine)
                self.assertEqual(set(facts), {"gpu", "driver", "mesa", "kernel", "clocks", "load"})
                found = gaps(facts)
                self.assertEqual(set(found), expected)
                for path, reason in found.items():
                    self.assertTrue(any(reason.startswith(name + ":") for name in record.MACHINE_COMMANDS),
                                    f"{path}: {reason!r} names no command")

    def test_build_facts_parses_its_inputs(self):
        built = record.build_facts(**build_args())
        self.assertEqual(built, {
            "shas": SHAS,
            "diff": DIFF,
            "binaries": {"build/hookshot": sha(b"exe"), "build/libhookshot.so": sha(b"lib")},
            "closure": CLOSURE,
            "env": ENV,
        })

    def test_build_facts_refuses_malformed_inputs(self):
        cases = {
            "no shas": {"shas": {}},
            "a short commit": {"shas": {"hookshot": SHAS["hookshot"][:39]}},
            "an upper-case commit": {"shas": {"hookshot": SHAS["hookshot"].upper()}},
            "a commit that is not text": {"shas": {"hookshot": None}},
            "a source with no name": {"shas": {"": SHAS["hookshot"]}},
            "a diff that is not text": {"diff": None},
            "no sha256sum output": {"sha256sum": ""},
            "a sha256sum hash of 63 digits": {"sha256sum": f"{'0' * 63}  build/hookshot\n"},
            "a sha256sum line with no path": {"sha256sum": f"{sha(b'exe')}\n"},
            "a binary hashed twice": {"sha256sum": SHA256SUM + SHA256SUM},
            "a blank line": {"sha256sum": SHA256SUM + "\n"},
            "a closure outside the store": {"closure": "/usr/lib/hookshot"},
            "a closure with no name": {"closure": "/nix/store/q4kx1h7v3d2m9b6n8c5p0r1s2t3u4w5y"},
            "a closure below a store path": {"closure": CLOSURE + "/bin"},
            "a closure that is not text": {"closure": None},
            "an env that is not a mapping": {"env": ["RX_VK_DEVICE=0"]},
            "an env value that is not text": {"env": {"RX_VK_DEVICE": 0}},
        }
        for label, changes in cases.items():
            with self.subTest(label), self.assertRaises(record.RecordError):
                record.build_facts(**build_args(**changes))

    def test_build_and_machine_facts_round_trip_through_write_run(self):
        root = self.base / "record"
        messages = []
        written = []
        for machine in (outputs(), outputs(nvidia_smi=None)):
            facts = {**record.build_facts(**build_args()), **record.machine_facts(0, machine)}
            written.append((record.write_run(root, facts, {"run.log": b"log"}, {}, journal=messages.append), facts))
        for (run_id, digest), facts in written:
            manifest, manifest_sha = record.read_manifest(root, run_id)
            self.assertEqual(manifest_sha, digest)
            self.assertEqual(set(manifest["facts"]), set(record.FACT_KEYS))
            self.assertEqual(manifest["facts"], facts)
        self.assertEqual(set(gaps(written[1][1])), {"driver.nvidia_smi", "clocks.gpu"})
        self.assertEqual(len(messages), 2)


def shot(n):
    """The bytes of fixture screenshot n"""
    return f"\x89PNG {n}".encode("utf-8")


class VerifyTest(unittest.TestCase):
    """hx verify on three chained fixture runs and the fixture journal file their writes anchored"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "record"
        self.journal = self.base / "journal.txt"
        self.lines = []

    def write(self, files=None, shots=None):
        """write_run into the test's root, anchored in the test's journal lines"""
        return record.write_run(self.root, facts(), files or {}, shots or {}, journal=self.lines.append)

    def three_runs(self):
        """Three chained runs, each with two run files (one nested) and a screenshot of its own; returns their ids"""
        return [self.write(files={"a.txt": f"run {n}\n".encode("utf-8"), "logs/out.txt": f"log {n}\n".encode("utf-8")},
                           shots={f"shot-{n}.png": shot(n)})[0]
                for n in (1, 2, 3)]

    def sync(self):
        """Writes the fixture journal file: the lines the writes anchored, as journalctl prints them"""
        self.journal.write_text("".join(line + "\n" for line in self.lines), encoding="utf-8")

    def anchor(self, run_id, digest=None):
        """The fixture journal with run_id's line naming digest; without a line for run_id when digest is None"""
        self.lines = [line for line in self.lines if not line.startswith(f"run={run_id} ")]
        if digest is not None:
            self.lines.append(record.journal_line(run_id, digest))

    def edit_manifest(self, run_id, change):
        """Rewrites a run's manifest.json with change applied to its parsed JSON; returns the new manifest's sha256"""
        path = self.root / "runs" / run_id / "manifest.json"
        manifest = json.loads(path.read_bytes())
        change(manifest)
        path.write_bytes((json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        return sha(path.read_bytes())

    def tree(self):
        """Every path under the test's root with the bytes of each file: what verify must leave as it found it"""
        return {p.relative_to(self.root).as_posix(): p.read_bytes() if p.is_file() else None
                for p in sorted(self.root.rglob("*"))}

    def run_hx(self, *args, env=None):
        return subprocess.run([sys.executable, "-I", "-B", str(HX), "verify", *args], capture_output=True, env=env)

    def verify(self):
        """hx verify on the test's root and its journal file, which is written from the journal lines first"""
        self.sync()
        return self.run_hx("--root", str(self.root), "--journal", str(self.journal))

    def problems(self, done, *prefixes):
        """The output of a run that exited 1 with stderr empty, where each prefix starts one of its problem lines"""
        self.assertEqual(done.returncode, 1, done.stderr.decode("utf-8", "replace"))
        self.assertEqual(done.stderr, b"")
        lines = done.stdout.decode("utf-8").splitlines()
        for prefix in prefixes:
            self.assertTrue(any(line.startswith(prefix + ":") for line in lines), f"no line names {prefix!r}: {lines}")
        return lines

    def test_a_clean_record_of_three_runs_with_its_journal_exits_0(self):
        self.three_runs()
        before = self.tree()
        done = self.verify()
        self.assertEqual((done.returncode, done.stderr), (0, b""))
        self.assertEqual(done.stdout, b"3 runs checked, no problems\n")
        self.assertEqual(self.tree(), before)

    def test_a_run_file_whose_bytes_changed_is_named(self):
        self.three_runs()
        (self.root / "runs" / "000002" / "files" / "logs" / "out.txt").write_bytes(b"edited\n")
        lines = self.problems(self.verify(), "runs/000002/files/logs/out.txt")
        self.assertEqual(len(lines), 1)

    def test_a_run_file_that_was_removed_is_named(self):
        self.three_runs()
        (self.root / "runs" / "000002" / "files" / "a.txt").unlink()
        lines = self.problems(self.verify(), "runs/000002/files/a.txt")
        self.assertEqual(len(lines), 1)

    def test_a_file_added_to_a_run_folder_is_named(self):
        self.three_runs()
        folder = self.root / "runs" / "000002"
        (folder / "files" / "extra.txt").write_bytes(b"added")
        (folder / "stray.txt").write_bytes(b"added")
        lines = self.problems(self.verify(), "runs/000002/files/extra.txt", "runs/000002/stray.txt")
        self.assertEqual(len(lines), 2)

    def test_a_changed_screenshot_is_named(self):
        self.three_runs()
        (self.root / "screenshots" / sha(shot(2))).write_bytes(b"damaged")
        lines = self.problems(self.verify(), f"screenshots/{sha(shot(2))}")
        self.assertEqual(len(lines), 1)
        self.assertIn("run 000002", lines[0])

    def test_a_removed_screenshot_is_named(self):
        self.three_runs()
        (self.root / "screenshots" / sha(shot(2))).unlink()
        lines = self.problems(self.verify(), f"screenshots/{sha(shot(2))}")
        self.assertEqual(len(lines), 1)
        self.assertIn("run 000002", lines[0])

    def test_an_edited_manifest_is_named_by_the_next_prior_and_the_journal(self):
        self.three_runs()
        self.edit_manifest("000001", lambda m: m["facts"].update(driver={"value": "edited"}))
        lines = self.problems(self.verify(), "run 000001", "run 000002")
        self.assertEqual(len(lines), 2)
        self.assertIn("prior", next(line for line in lines if line.startswith("run 000002:")))

    def test_the_latest_manifest_edited_is_named_by_the_journal(self):
        self.three_runs()
        self.edit_manifest("000003", lambda m: m["facts"].update(driver={"value": "edited"}))
        lines = self.problems(self.verify(), "run 000003")
        self.assertEqual(len(lines), 1)

    def test_a_middle_run_folder_removed_breaks_the_chain(self):
        self.three_runs()
        shutil.rmtree(self.root / "runs" / "000002")
        lines = self.problems(self.verify(), "run 000003", "run 000002")
        self.assertEqual(len(lines), 2)
        self.assertIn("prior names run 000002, but the run before it is run 000001",
                      next(line for line in lines if line.startswith("run 000003:")))

    def test_a_prior_that_names_another_hash_is_named(self):
        self.three_runs()
        digest = self.edit_manifest("000003", lambda m: m["prior"].update(sha256="0" * 64))
        self.anchor("000003", digest)
        lines = self.problems(self.verify(), "run 000003")
        self.assertEqual(len(lines), 1)
        self.assertIn("prior", lines[0])

    def test_a_first_run_with_a_prior_is_named(self):
        self.three_runs()
        shutil.rmtree(self.root / "runs" / "000001")
        self.anchor("000001")
        lines = self.problems(self.verify(), "run 000002")
        self.assertEqual(len(lines), 1)

    def test_a_run_in_the_journal_but_not_the_record_is_named(self):
        self.three_runs()
        shutil.rmtree(self.root / "runs" / "000003")
        lines = self.problems(self.verify(), "run 000003")
        self.assertEqual(len(lines), 1)

    def test_a_manifest_that_no_longer_parses_is_named(self):
        self.three_runs()
        (self.root / "runs" / "000002" / "manifest.json").write_bytes(b"{not json")
        self.problems(self.verify(), "run 000002", "run 000003")

    def test_a_manifest_of_another_shape_is_named(self):
        self.three_runs()
        (self.root / "runs" / "000002" / "manifest.json").write_bytes(b"[]\n")
        self.problems(self.verify(), "run 000002")

    def test_an_unreadable_journal_file_or_a_missing_root_exits_2(self):
        self.three_runs()
        self.sync()
        absent_journal = self.base / "absent.txt"
        absent_root = self.base / "absent"
        cases = {
            "a journal file that does not exist": (["--root", str(self.root), "--journal", str(absent_journal)],
                                                   absent_journal),
            "a journal that is a folder": (["--root", str(self.root), "--journal", str(self.base)], self.base),
            "a root that does not exist": (["--root", str(absent_root), "--journal", str(self.journal)], absent_root),
        }
        for label, (args, named) in cases.items():
            with self.subTest(label):
                done = self.run_hx(*args)
                self.assertEqual(done.returncode, 2)
                self.assertEqual(done.stdout, b"")
                self.assertIn(str(named), done.stderr.decode("utf-8", "replace"))

    def test_the_default_root_is_under_the_home_folder(self):
        home = self.base / "home"
        record.write_run(home / "hookshot-harness" / "results", facts(), {"a.txt": b"home"}, {},
                         journal=self.lines.append)
        self.sync()
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
        done = self.run_hx("--journal", str(self.journal), env=env)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"1 run checked, no problems\n", b""))


if __name__ == "__main__":
    unittest.main()
