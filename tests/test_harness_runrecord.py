"""scripts/harness/runrecord.py against fixtures with neutral names: a run's facts come from a build's build.txt, its dirty
diff, its sha256sum output, its environment and the command outputs of a machine; one run is written under the record
root's results folder, and its id and manifest hash are returned. Checks: no fact or file name in the manifest has the
fixture home path or user name, and the environment and diff are scrubbed (R7); hx verify's check passes the run against
the collected journal (R7); a GPU is picked by its device name, and an absent or ambiguous device gives the facts' gaps;
a failing or missing machine command is None; a refusal from record.py is raised. Fakes only: no command, GPU, journal or
host is used.

python -I -B tests/test_harness_runrecord.py
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "harness"))
import hx_verify
import record
import runrecord

HOME = "/srv/fixture-home/tester7"
USER = "tester7"
GPU = "NVIDIA GeForce RTX 3080"
SHA = "3f9c2a1b7d4e6f80a1b2c3d4e5f60718293a4b5c"
CLOSURE = "/nix/store/q4kx1h7v3d2m9b6n8c5p0r1s2t3u4w5y-hookshot-1.0"
DIFF = ("diff --git a/scripts/harness/record.py b/scripts/harness/record.py\n"
        "--- a/scripts/harness/record.py\n+++ b/scripts/harness/record.py\n"
        f"@@ -1 +1,2 @@\n docstring\n+# scratch copy at {HOME}/src\n")
ENV = {"HOME": HOME, "RX_VK_DEVICE": "0"}


def sha(data):
    return record.sha256_bytes(data)


# The build's record as hx build writes it: its build line names a folder under the home path.
BUILD_TXT = "".join(f"{key}: {value}\n" for key, value in (
    ("closure", CLOSURE), ("sha", SHA), ("dirty", sha(DIFF.encode("utf-8"))), ("hookshot", sha(b"exe")),
    ("hookshot_vk.so", sha(b"lib")), ("build", f"{HOME}/harness/builds/{SHA[:12]}"),
    ("job", "build-20261113T090000Z-a1b2c3")))
SHA256SUM = f"{sha(b'exe')}  build/hookshot\n{sha(b'lib')}  build/hookshot_vk.so\n"

# Command outputs as a machine prints them: an NVIDIA GPU 0 beside an Intel GPU 1 with Mesa. The identifiers are made up.
VULKANINFO = "\n".join([
    "==========",
    "VULKANINFO",
    "==========",
    "",
    "Vulkan Instance Version: 1.3.275",
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
    f"\tdeviceName         = {GPU}",
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
KERNEL = f"Linux version 6.8.0-45-generic ({USER}@example.invalid) (gcc (GCC) 13.2.0) #47-generic SMP\n"
LOAD = "0.52 0.58 0.59 1/1234 5678\n"
CPU_CLOCKS = "".join(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq:{khz}\n"
                     for i, khz in enumerate((3600000, 3000000, 1400000, 1400000)))


def command_outputs(**changes):
    """A machine's command outputs by MACHINE_COMMANDS name; a change sets a name to its text or to None (failed)"""
    found = {"vulkaninfo": VULKANINFO, "nvidia_smi": NVIDIA_SMI, "kernel": KERNEL, "load": LOAD,
             "cpu_clocks": CPU_CLOCKS}
    found.update(changes)
    return found


def gaps(value, path=()):
    """Each gap in a facts value, by dotted path: a dict whose only key is 'gap' is a gap"""
    if not isinstance(value, dict):
        return {}
    if set(value) == {"gap"}:
        return {".".join(path): value["gap"]}
    found = {}
    for key, inner in value.items():
        found.update(gaps(inner, path + (key,)))
    return found


class RunRecordTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.harness = Path(tmp.name) / "harness"
        self.lines = []

    def facts(self, **changes):
        """runrecord.facts for the fixture build on the fixture machine; a change replaces one of its arguments"""
        args = {"build_txt": BUILD_TXT, "diff": DIFF, "sha256sum": SHA256SUM, "env": ENV, "expect": GPU,
                "outputs": command_outputs(), "home": HOME, "user": USER}
        args.update(changes)
        return runrecord.facts(**args)

    def test_a_run_is_written_under_results_with_every_fact_and_hx_verify_passes_it(self):
        run_id, digest = runrecord.write(self.harness, self.facts(), {"logs/run.log": b"log\n"},
                                         journal=self.lines.append)
        self.assertEqual(run_id, "000001")
        manifest_bytes = (self.harness / "results" / "runs" / run_id / "manifest.json").read_bytes()
        self.assertEqual(digest, sha(manifest_bytes))
        self.assertEqual(set(json.loads(manifest_bytes)["facts"]), set(record.FACT_KEYS))
        self.assertEqual(self.lines, [record.journal_line(run_id, digest)])
        journal = record.parse_journal("".join(line + "\n" for line in self.lines))
        self.assertEqual(hx_verify.check(runrecord.results_dir(self.harness), journal), ([], 1))

    def test_no_fact_or_file_name_in_the_manifest_has_the_home_path_or_user_name(self):
        run_id, _ = runrecord.write(self.harness, self.facts(), {"logs/run.log": b"log\n"},
                                    journal=self.lines.append)
        text = (self.harness / "results" / "runs" / run_id / "manifest.json").read_text(encoding="utf-8")
        self.assertNotIn(HOME, text)
        self.assertNotIn(USER, text)
        self.assertEqual(set(json.loads(text)["files"]), {"logs/run.log"})

    def test_the_environment_and_diff_are_scrubbed_and_the_build_line_is_not_passed(self):
        facts = self.facts()
        self.assertEqual(set(facts), set(record.FACT_KEYS))
        self.assertEqual(facts["env"], {"HOME": "~", "RX_VK_DEVICE": "0"})
        self.assertIn("+# scratch copy at ~/src\n", facts["diff"])
        self.assertNotIn(HOME, facts["diff"])
        self.assertEqual(facts["shas"], {"hookshot": SHA})
        self.assertEqual(facts["closure"], CLOSURE)
        self.assertEqual(facts["binaries"], {"build/hookshot": sha(b"exe"), "build/hookshot_vk.so": sha(b"lib")})

    def test_scrub_replaces_the_home_path_then_the_user_name(self):
        self.assertEqual(runrecord.scrub(f"{HOME}/a {USER}", HOME, USER), "~/a user")
        self.assertEqual(runrecord.scrub("kept", "", ""), "kept")

    def test_results_dir_is_the_results_folder_under_the_harness_root(self):
        self.assertEqual(runrecord.results_dir(self.harness), self.harness / "results")

    def test_gpu_index_picks_the_block_by_device_name(self):
        self.assertEqual(runrecord.gpu_index(VULKANINFO, GPU), 0)
        self.assertEqual(runrecord.gpu_index(VULKANINFO, "Intel(R) UHD Graphics 770 (RPL-S)"), 1)
        self.assertIsNone(runrecord.gpu_index(VULKANINFO, "Absent Device"))
        self.assertIsNone(runrecord.gpu_index(None, GPU))

    def test_two_gpus_of_one_name_give_no_index(self):
        twin = VULKANINFO + f"GPU2:\n\tdeviceName         = {GPU}\n"
        self.assertIsNone(runrecord.gpu_index(twin, GPU))

    def test_an_absent_device_gives_the_facts_gaps(self):
        facts = self.facts(expect="Absent Device")
        self.assertEqual(set(facts), set(record.FACT_KEYS))
        self.assertEqual(set(gaps(facts)), {"gpu", "driver", "clocks.gpu"})

    def test_a_failing_or_missing_machine_command_is_none(self):
        calls = []
        texts = {tuple(record.MACHINE_COMMANDS[name]): text.encode("utf-8")
                 for name, text in (("kernel", KERNEL), ("load", LOAD), ("cpu_clocks", CPU_CLOCKS))}

        def fake(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "nvidia-smi":
                return subprocess.CompletedProcess(argv, 9, b"", b"no driver")
            if argv[0] == "vulkaninfo":
                raise FileNotFoundError(2, "No such file or directory", argv[0])
            return subprocess.CompletedProcess(argv, 0, texts[tuple(argv)], b"")

        self.assertEqual(runrecord.machine_outputs(fake), {
            "nvidia_smi": None, "vulkaninfo": None, "kernel": KERNEL, "load": LOAD, "cpu_clocks": CPU_CLOCKS})
        self.assertEqual(calls, list(record.MACHINE_COMMANDS.values()))

    def test_a_record_refusal_is_raised(self):
        with self.assertRaises(record.RecordError):
            self.facts(diff=None)
        partial = self.facts()
        del partial["load"]
        with self.assertRaises(record.RecordError):
            runrecord.write(self.harness, partial, {}, journal=self.lines.append)

        def failing_journal(message):
            raise RuntimeError("no journal here")

        with self.assertRaises(record.RecordError):
            runrecord.write(self.harness, self.facts(), {}, journal=failing_journal)

    def test_a_build_txt_that_is_not_key_value_or_lacks_a_sha_or_closure_is_refused(self):
        without = {key: "".join(line for line in BUILD_TXT.splitlines(True) if not line.startswith(key + ":"))
                   for key in ("sha", "closure")}
        cases = {"a line with no colon": BUILD_TXT + "garbage\n", "no sha line": without["sha"],
                 "no closure line": without["closure"]}
        for label, text in cases.items():
            with self.subTest(label), self.assertRaises(runrecord.RunRecordError):
                self.facts(build_txt=text)


if __name__ == "__main__":
    unittest.main()
