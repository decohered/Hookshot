"""A job's game on the build host, under the run conditions: started with an optional save, its control bridge awaited, its
process kept for its exit status, its live input and hour written, quit, and its outputs collected as bytes. Standard
library only.

The job folder holds run/ (the socket, the log, the live-input file, the frame log) and data/ (the saves the game reads).
The frame trace is written beside the game's binary, so it sits in the game folder. The game starts as
scripts/hookshot_ctl.py's launch starts it.

    game = start(job, game_dir, device_env, save=None, slot=None, extra=None)
    game.set_hour(12)
    game.live("!synclog")
    status = game.quit(timeout=60)
    outputs = game.outputs()
"""
import importlib.util
import os
import subprocess
import time
from pathlib import Path

import jobs

FIXED_TIME = "1700000000"   # (HOS_FIXED_TIME: the clock starts at this POSIX time at boot, so that runs can be compared)

_CTL = Path(__file__).resolve().parents[1] / "hookshot_ctl.py"
_spec = importlib.util.spec_from_file_location("hookshot_ctl", _CTL)
ctl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ctl)


class GameError(Exception):
    pass


def run_env(job, device_env, extra=None):
    """The game's environment for a job: the run conditions, then the job's and the device's variables (jobs.launch_env),
    then extra. A later one wins, so the device's variables always reach the game"""
    run = os.path.join(job, "run")
    env = {
        "HOS_HEADLESS": "1",
        "HOS_REALTIME_STEP": "0",
        "HOS_DYNRES": "0",
        "HOS_FIXED_TIME": FIXED_TIME,
        "HOS_AUDIO": "0",
        "RX_VK_FRAME_TRACE": "1",
        "RX_VK_FRAME_LOG": os.path.join(run, "frametimes.csv"),
        "HOS_INPUT_LIVE": os.path.join(run, "live.txt"),
        "HOS_CONTROL": os.path.join(run, "control.sock"),
    }
    env.update(jobs.launch_env(job, device_env))
    env.update(extra or {})
    return env


class Game:
    """A started game: its process, its bridge's socket, its live-input file and its outputs by name"""

    def __init__(self, proc, sock, log, live, files):
        self.proc = proc
        self.pid = proc.pid
        self.sock = sock
        self.log = log
        self.live_path = live
        self.files = files

    def connect(self):
        """A connection to the game's control bridge"""
        try:
            return ctl.Bridge(self.sock)
        except ctl.Failed as e:
            raise GameError(str(e)) from e

    def live(self, line):
        """Appends a line to the live-input file, which the game reads every 30 ms"""
        with open(self.live_path, "a") as f:
            f.write(line + "\n")

    def set_hour(self, hour):
        """The game's hour, by the time action the Cheats page sends (!cheat 100 + hour, for 0 to 23)"""
        if not (isinstance(hour, int) and 0 <= hour <= 23):
            raise GameError("hour: 0 to 23, not %r" % (hour,))
        self.live("!cheat %d" % (100 + hour))

    def quit(self, timeout=60):
        """Asks the game to quit and returns its exit status once it exits, within timeout seconds. A game that does not exit
        is killed and raises GameError"""
        if self.proc.poll() is None:
            self._ask_quit()
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            _kill(self.proc)
            raise GameError("the game did not quit in %.0f s: killed" % timeout)

    def _ask_quit(self):
        try:
            bridge = self.connect()
        except GameError:
            return   # (no bridge: the wait in quit says whether the game went on its own)
        try:
            bridge.request("quit")
        except ctl.Failed:
            pass   # (no reply: the wait in quit says whether the game quit)
        finally:
            bridge.close()

    def _read(self):
        """(the outputs that exist as {name: bytes}, the names of those that do not)"""
        found, gone = {}, []
        for name, path in self.files.items():
            try:
                with open(path, "rb") as f:
                    found[name] = f.read()
            except FileNotFoundError:
                gone.append(name)
        return found, gone

    def outputs(self):
        """The outputs that exist, by name: log, frame_trace and frame_log, as bytes"""
        return self._read()[0]

    def missing(self):
        """The names of the outputs that do not exist"""
        return self._read()[1]


def start(job, game_dir, device_env, save=None, slot=None, extra=None, timeout=1800, popen=subprocess.Popen):
    """The game in game_dir under run_env(job, device_env, extra), started as scripts/hookshot_ctl.py's launch starts it: its
    stdout and stderr to the job's run/log.txt, in a session of its own, stdin from /dev/null. With save, that save folder is
    copied into the job's data first (with slot, that slot alone). Returns once the bridge answers status with ok. A game
    that ends first raises GameError naming its log; one whose bridge does not answer within timeout seconds is killed and
    raises the same."""
    if slot is not None and save is None:
        raise GameError("slot goes with save")
    job = os.path.abspath(job)
    game = os.path.abspath(game_dir)
    exe = os.path.join(game, "hookshot")
    if not os.path.isfile(exe):
        raise GameError("no game at %s" % exe)
    os.makedirs(os.path.join(job, "run"), mode=0o700, exist_ok=True)
    env = dict(os.environ)
    env.update(run_env(job, device_env, extra))
    if save is not None:
        try:
            ctl.copy_save(save, env["XDG_DATA_HOME"], slot)
        except ctl.Failed as e:
            raise GameError(str(e)) from e
    log = os.path.join(job, "run", "log.txt")
    with open(log, "wb") as out:
        try:
            proc = popen([exe], cwd=game, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         start_new_session=True)
        except OSError as e:
            raise GameError("the game would not start (%s): %s" % (exe, e)) from e
    sock = env["HOS_CONTROL"]
    try:
        _await_bridge(proc, sock, timeout, log)
    except BaseException:
        _kill(proc)   # (no Game is returned for it: a game left running would keep its socket and its folder)
        raise
    files = {
        "log": log,
        "frame_trace": os.path.join(game, "hookshot_frames.csv"),
        "frame_log": env["RX_VK_FRAME_LOG"],
    }
    return Game(proc, sock, log, env["HOS_INPUT_LIVE"], files)


def _await_bridge(proc, sock, timeout, log):
    """Returns once the bridge on sock answers; raises GameError when the game ends first or the timeout passes"""
    deadline = time.monotonic() + timeout
    while not _answers(sock):
        if proc.poll() is not None:
            raise GameError("the game ended (%d) before its bridge answered: %s" % (proc.returncode, log))
        if time.monotonic() > deadline:
            raise GameError("the game's bridge did not answer in %.0f s: %s" % (timeout, log))
        time.sleep(0.2)


def _answers(sock):
    """Whether the bridge on sock answers status with ok"""
    try:
        bridge = ctl.Bridge(sock, 5)
    except ctl.Failed:
        return False
    try:
        return bool(bridge.request("status").get("ok"))
    except ctl.Failed:
        return False
    finally:
        bridge.close()


def _kill(proc):
    proc.kill()
    proc.wait()
