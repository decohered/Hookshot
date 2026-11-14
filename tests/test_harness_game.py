"""scripts/harness/game.py, a job's game started under the run conditions, waited for, driven and quit, against a stand-in
game: a Python script named hookshot that answers status and quit on $HOS_CONTROL as tests/test_hookshot_ctl.py's stand-in
does, writes a frame trace beside itself and a frame log where RX_VK_FRAME_LOG says, records its start (its pid, folder and
environment) and each request, and exits with the status FAKE_EXIT names. Checks: the run conditions, the device's and the
extra variables reach it, and a save folder is copied into the job's data (run_env, start); start returns once the bridge
answers, a game that ends first raises naming its log, and a game whose bridge never answers is killed at the timeout;
set_hour and live append their lines; quit returns the exit status (3 included), a game that already quit gives its status,
and a game that does not quit is killed; outputs returns the log, trace and frame log as bytes, and missing names an absent
one. Linux only: a Unix socket and a shebang script.

python -I -B tests/test_harness_game.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RUN_ALL_SKIP = {"windows": "the stand-in game is a POSIX script started through its shebang, and its bridge a Unix socket"}

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "harness"))
sys.path.insert(0, str(ROOT / "scripts"))
import game as game_mod
import hookshot_ctl as ctl

FAKE = r'''
import json, os, socket, sys, threading, time
env = os.environ
folder = os.path.dirname(os.path.abspath(__file__))
def note(obj):
    with open(env["FAKE_LOG"], "a") as f:
        f.write(json.dumps(obj) + "\n")
note({"cmd": "_start", "pid": os.getpid(), "cwd": os.getcwd(), "env": dict(env)})
print("fake game: started", flush=True)
if env.get("FAKE_START_EXIT"):
    sys.exit(int(env["FAKE_START_EXIT"]))
if env.get("FAKE_SILENT"):
    time.sleep(120)
time.sleep(float(env.get("FAKE_BIND_DELAY", "0")))
with open(os.path.join(folder, "hookshot_frames.csv"), "wb") as f:
    f.write(b"frame,ms\n1,16.7\n")
with open(env["RX_VK_FRAME_LOG"], "wb") as f:
    f.write(b"frame,seconds,ms\n1,0.0,16.7\n")
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(env["HOS_CONTROL"])
srv.listen(4)
def serve(conn):
    f = conn.makefile("rwb", buffering=0)
    for raw in f:
        req = json.loads(raw)
        note({"cmd": req.get("cmd")})
        rid = req.get("id")
        if req.get("cmd") == "status":
            reply = {"id": rid, "ok": True, "frame": 1}
        elif req.get("cmd") == "quit":
            f.write((json.dumps({"id": rid, "ok": True, "quitting": True}) + "\n").encode())
            if not env.get("FAKE_HANG"):
                os._exit(int(env.get("FAKE_EXIT", "0")))
            continue
        else:
            reply = {"id": rid, "ok": False, "error": "unknown_cmd", "message": "no command"}
        f.write((json.dumps(reply) + "\n").encode())
while True:
    threading.Thread(target=serve, args=(srv.accept()[0],), daemon=True).start()
'''


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def reap(game):
    if game.proc.poll() is None:
        game.proc.kill()
        game.proc.wait()


def kill_if_alive(pid):
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass


class GameTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="game-", dir="/tmp" if os.path.isdir("/tmp") else None)
        self.addCleanup(tmp.cleanup)
        self.base = tmp.name
        self.game = os.path.join(self.base, "game")
        self.log = os.path.join(self.base, "requests.jsonl")
        os.mkdir(self.game)
        exe = os.path.join(self.game, "hookshot")
        write(exe, ("#!%s -I\n%s" % (sys.executable, FAKE)).encode())
        os.chmod(exe, 0o755)
        self.job = self.new_job("job")
        self.set_env({"FAKE_LOG": self.log})

    def new_job(self, name):
        job = os.path.join(self.base, name)
        os.makedirs(os.path.join(job, "data"))
        return job

    def set_env(self, values):
        patch = mock.patch.dict(os.environ, values)
        patch.start()
        self.addCleanup(patch.stop)

    def start(self, job=None, device_env=None, **kw):
        game = game_mod.start(job or self.job, self.game, device_env or {}, **kw)
        self.addCleanup(reap, game)
        return game

    def records(self):
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def started(self):
        """The one start the stand-in recorded: its pid, folder and environment"""
        starts = [r for r in self.records() if r["cmd"] == "_start"]
        self.assertEqual(len(starts), 1)
        return starts[0]

    def make_saves(self):
        src = os.path.join(self.base, "saves")
        os.makedirs(os.path.join(src, "slot_00"))
        os.makedirs(os.path.join(src, "slot_03"))
        write(os.path.join(src, "account.sav"), b"account")
        write(os.path.join(src, "option.sav"), b"option")
        write(os.path.join(src, "slot_00", "progress.sav"), b"zero")
        write(os.path.join(src, "slot_03", "progress.sav"), b"three")
        return src

    def test_run_env_has_the_run_conditions(self):
        env = game_mod.run_env(self.job, {"RX_VK_PACE": "2"}, {"HOS_SKIP_OPENING": "1"})
        run = os.path.join(self.job, "run")
        self.assertEqual(env["HOS_HEADLESS"], "1")
        self.assertEqual(env["HOS_REALTIME_STEP"], "0")
        self.assertEqual(env["HOS_DYNRES"], "0")
        self.assertRegex(env["HOS_FIXED_TIME"], r"^[0-9]+$")
        self.assertEqual(env["HOS_AUDIO"], "0")
        self.assertEqual(env["HOOKSHOT_NO_UPDATE_CHECK"], "1")
        self.assertEqual(env["RX_VK_FRAME_TRACE"], "1")
        self.assertEqual(env["RX_VK_FRAME_LOG"], os.path.join(run, "frametimes.csv"))
        self.assertEqual(env["HOS_INPUT_LIVE"], os.path.join(run, "live.txt"))
        self.assertEqual(env["HOS_CONTROL"], os.path.join(run, "control.sock"))
        self.assertEqual(env["XDG_DATA_HOME"], os.path.join(self.job, "data"))
        self.assertEqual(env["RX_VK_PACE"], "2")
        self.assertEqual(env["HOS_SKIP_OPENING"], "1")

    def test_the_stand_in_sees_the_run_conditions_the_device_and_the_extra_variables(self):
        device = {"RX_VK_PACE": "2", "RX_TEST_DEVICE": "gpu-a"}
        extra = {"HOS_SKIP_OPENING": "1"}
        self.start(device_env=device, extra=extra)
        seen = self.started()
        for name, value in game_mod.run_env(self.job, device, extra).items():
            self.assertEqual(seen["env"].get(name), value, name)
        self.assertEqual(seen["env"]["HOS_HEADLESS"], "1")
        self.assertEqual(seen["env"]["RX_VK_FRAME_TRACE"], "1")
        self.assertEqual(seen["env"]["RX_TEST_DEVICE"], "gpu-a")
        self.assertEqual(seen["env"]["HOS_SKIP_OPENING"], "1")
        self.assertEqual(os.path.realpath(seen["cwd"]), os.path.realpath(self.game))

    def test_a_save_slot_is_copied_into_the_jobs_data(self):
        src = self.make_saves()
        self.start(save=src, slot="03")
        saves = os.path.join(self.job, "data", "hookshot", "save", ctl.SAVE_USER)
        self.assertEqual(sorted(os.listdir(saves)), ["account.sav", "option.sav", "slot_03"])
        with open(os.path.join(saves, "slot_03", "progress.sav"), "rb") as f:
            self.assertEqual(f.read(), b"three")
        self.assertEqual(self.started()["env"]["XDG_DATA_HOME"], os.path.join(self.job, "data"))

    def test_a_save_folder_without_a_slot_is_copied_whole(self):
        src = self.make_saves()
        job = self.new_job("job-all")
        self.start(job=job, save=src)
        saves = os.path.join(job, "data", "hookshot", "save", ctl.SAVE_USER)
        self.assertEqual(sorted(os.listdir(saves)), ["account.sav", "option.sav", "slot_00", "slot_03"])

    def test_start_refuses_a_slot_without_a_save(self):
        with self.assertRaisesRegex(game_mod.GameError, "slot goes with save"):
            game_mod.start(self.job, self.game, {}, slot="03")

    def test_start_refuses_a_slot_the_save_does_not_have(self):
        with self.assertRaisesRegex(game_mod.GameError, "has no slot_07"):
            game_mod.start(self.job, self.game, {}, save=self.make_saves(), slot="07")

    def test_start_refuses_a_folder_without_the_game(self):
        empty = os.path.join(self.base, "empty")
        os.mkdir(empty)
        with self.assertRaisesRegex(game_mod.GameError, "no game at"):
            game_mod.start(self.job, empty, {})

    def test_start_returns_once_the_bridge_answers(self):
        self.set_env({"FAKE_BIND_DELAY": "1"})
        game = self.start()
        self.assertEqual(game.pid, self.started()["pid"])
        self.assertEqual(game.sock, os.path.join(self.job, "run", "control.sock"))
        self.assertEqual(game.log, os.path.join(self.job, "run", "log.txt"))
        self.assertIsNone(game.proc.poll())
        bridge = game.connect()
        try:
            self.assertTrue(bridge.request("status")["ok"])
        finally:
            bridge.close()

    def test_a_game_that_ends_before_its_bridge_answers_raises_naming_its_log(self):
        self.set_env({"FAKE_START_EXIT": "3"})
        with self.assertRaises(game_mod.GameError) as caught:
            game_mod.start(self.job, self.game, {}, timeout=20)
        self.assertRegex(str(caught.exception), r"ended \(3\)")
        self.assertIn(os.path.join(self.job, "run", "log.txt"), str(caught.exception))

    def test_a_game_whose_bridge_never_answers_is_killed_at_the_timeout(self):
        self.set_env({"FAKE_SILENT": "1"})
        with self.assertRaises(game_mod.GameError) as caught:
            game_mod.start(self.job, self.game, {}, timeout=1)
        self.assertIn(os.path.join(self.job, "run", "log.txt"), str(caught.exception))
        pid = self.started()["pid"]
        self.addCleanup(kill_if_alive, pid)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_set_hour_and_live_append_lines_to_the_live_file(self):
        game = self.start()
        live = os.path.join(self.job, "run", "live.txt")
        game.set_hour(0)
        game.set_hour(5)
        game.set_hour(23)
        game.live("!synclog")
        with open(live, "rb") as f:
            before = f.read()
        self.assertEqual(before, b"!cheat 100\n!cheat 105\n!cheat 123\n!synclog\n")
        for hour in (24, -1, "5", 5.0):
            with self.subTest(hour=hour), self.assertRaises(game_mod.GameError):
                game.set_hour(hour)
        with open(live, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_quit_returns_the_exit_status_3_included(self):
        self.set_env({"FAKE_EXIT": "3"})
        game = self.start()
        self.assertEqual(game.quit(timeout=20), 3)
        self.assertEqual(self.records()[-1]["cmd"], "quit")

    def test_quit_of_a_game_that_already_quit_returns_its_status(self):
        self.set_env({"FAKE_EXIT": "3"})
        game = self.start()
        bridge = game.connect()
        try:
            bridge.request("quit")
        finally:
            bridge.close()
        self.assertEqual(game.quit(timeout=20), 3)

    def test_a_game_that_does_not_quit_is_killed(self):
        self.set_env({"FAKE_HANG": "1"})
        game = self.start()
        with self.assertRaisesRegex(game_mod.GameError, "did not quit"):
            game.quit(timeout=1)
        with self.assertRaises(ProcessLookupError):
            os.kill(game.pid, 0)

    def test_outputs_return_the_log_trace_and_frame_log_as_bytes(self):
        game = self.start()
        self.assertEqual(game.quit(timeout=20), 0)
        outputs = game.outputs()
        self.assertEqual(sorted(outputs), ["frame_log", "frame_trace", "log"])
        self.assertIn(b"fake game: started\n", outputs["log"])
        self.assertEqual(outputs["frame_trace"], b"frame,ms\n1,16.7\n")
        self.assertEqual(outputs["frame_log"], b"frame,seconds,ms\n1,0.0,16.7\n")
        self.assertEqual(game.missing(), [])

    def test_a_missing_output_is_named(self):
        game = self.start()
        game.quit(timeout=20)
        os.remove(os.path.join(self.game, "hookshot_frames.csv"))
        self.assertEqual(sorted(game.outputs()), ["frame_log", "log"])
        self.assertEqual(game.missing(), ["frame_trace"])

    def test_start_runs_the_game_as_launch_does(self):
        spawned = []

        def popen(args, **kw):
            spawned.append((args, kw))
            return subprocess.Popen(args, **kw)

        game = game_mod.start(self.job, self.game, {}, popen=popen)
        self.addCleanup(reap, game)
        ((args, kw),) = spawned
        self.assertEqual(args, [os.path.join(self.game, "hookshot")])
        self.assertEqual(kw["cwd"], self.game)
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertIs(kw["stderr"], subprocess.STDOUT)
        self.assertIs(kw["start_new_session"], True)
        self.assertEqual(kw["stdout"].name, os.path.join(self.job, "run", "log.txt"))


if __name__ == "__main__":
    unittest.main()
