"""scripts/harness/bridge.py, the harness's session over one control connection, against a scripted fake connection and a
fake clock. Checks: arrival by teleport, confirmed by the reported map with no load or event pending (a refusal, a wrong
map, a map that never arrives, and a load or event that never ends each raise, naming what failed, and nothing is
retried); the 8 s gap from the end of a teleport or age switch, however it ended, to the next, by the clock; an age
switch waiting for its load, and an age already set sending nothing more; the renderer being the expected device; a
window that fails on a map change or a load and returns both its statuses when steady; and entering play: the status
polled until menu or play within its timeout, then the press limit. No socket, game, GPU or host is used.

python -I -B tests/test_harness_bridge.py
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "harness"))
from bridge import BridgeError, Session

DEVICE = "device_a"
RENDERER = "%s (Vulkan, native)" % DEVICE


class FakeClock:
    """A clock that moves only when it is slept on, or when a test sets it"""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class FakeConn:
    """Answers each request from the script: a command's replies in order, its last reply again once they run out. A
    command the script does not name fails the test. Each request is kept with the clock's time when it was sent, and
    the clock moves by the seconds `takes` gives its command before it is answered."""

    def __init__(self, clock, script, takes=None):
        self.clock = clock
        self.script = {cmd: list(replies) for cmd, replies in script.items()}
        self.takes = takes or {}
        self.sent = []

    def request(self, cmd, timeout=None, **args):
        self.sent.append((cmd, args, self.clock()))
        if cmd not in self.script:
            raise AssertionError("unscripted request: %s %r" % (cmd, args))
        self.clock.now += self.takes.get(cmd, 0)
        replies = self.script[cmd]
        return replies.pop(0) if len(replies) > 1 else replies[0]


def requests(conn):
    return [(cmd, args) for cmd, args, _ in conn.sent]


def session(script, takes=None):
    clock = FakeClock()
    conn = FakeConn(clock, script, takes)
    return Session(conn, clock=clock, sleep=clock.sleep), conn, clock


def status(map_name="map_a", state="play", event=False, loading=False, renderer=RENDERER, frame=100):
    return {"ok": True, "frame": frame, "state": state, "map": map_name, "renderer": renderer, "replayed": frame - 1,
            "game": {"event": event, "loading": loading, "age": "age_a"}}


def ok(**fields):
    return {"ok": True, "frame": 100, **fields}


def failed(error, message):
    return {"ok": False, "frame": 100, "error": error, "message": message}


class ArriveTests(unittest.TestCase):
    def test_a_teleport_followed_by_the_expected_map_returns_it(self):
        s, conn, _ = session({
            "teleport": [ok(sent=True, map="map_b")],
            "wait": [ok(state="play", waited=187, map="map_b")],
            "status": [status("map_b")],
        })
        self.assertEqual(s.arrive("dest_a"), "map_b")
        self.assertEqual(requests(conn), [("teleport", {"to": "dest_a"}), ("wait", {"event": "map", "map": "map_b"}),
                                          ("status", {})])

    def test_a_refused_teleport_raises_naming_the_refusal_and_is_not_retried(self):
        s, conn, _ = session({"teleport": [failed("refused", "an event is playing")]})
        with self.assertRaises(BridgeError) as raised:
            s.arrive("dest_a")
        message = str(raised.exception)
        self.assertIn("refused", message)
        self.assertIn("an event is playing", message)
        self.assertEqual(requests(conn), [("teleport", {"to": "dest_a"})])

    def test_a_map_that_never_arrives_raises_naming_the_timeout(self):
        s, _, _ = session({
            "teleport": [ok(sent=True, map="map_b")],
            "wait": [failed("timeout", "no map change")],
        })
        with self.assertRaises(BridgeError) as raised:
            s.arrive("dest_a")
        self.assertIn("timeout", str(raised.exception))

    def test_a_wrong_map_raises_naming_both_maps(self):
        s, _, _ = session({
            "teleport": [ok(sent=True, map="map_b")],
            "wait": [ok(state="play", waited=5, map="map_b")],
            "status": [status("map_a")],
        })
        with self.assertRaises(BridgeError) as raised:
            s.arrive("dest_a")
        message = str(raised.exception)
        self.assertIn("map_a", message)
        self.assertIn("map_b", message)

    def test_a_load_that_never_ends_raises_naming_it(self):
        s, _, clock = session({
            "teleport": [ok(sent=True, map="map_b")],
            "wait": [ok(state="play", waited=5, map="map_b")],
            "status": [status("map_b", loading=True)],
        })
        with self.assertRaises(BridgeError) as raised:
            s.arrive("dest_a")
        self.assertIn("loading True", str(raised.exception))
        self.assertGreaterEqual(clock.now, 60)

    def test_an_event_that_never_ends_raises_naming_it(self):
        s, _, _ = session({
            "teleport": [ok(sent=True, map="map_b")],
            "wait": [ok(state="play", waited=5, map="map_b")],
            "status": [status("map_b", event=True)],
        })
        with self.assertRaises(BridgeError) as raised:
            s.arrive("dest_a")
        self.assertIn("event True", str(raised.exception))

    def test_an_age_is_switched_before_the_teleport_and_the_gap_applies_after_it(self):
        s, conn, clock = session({
            "age": [ok(age="age_b", sent=True, map="map_c")],
            "wait": [ok(state="play", waited=900, map="map_c"), ok(state="play", waited=5, map="map_b")],
            "teleport": [ok(sent=True, map="map_b")],
            "status": [status("map_b")],
        })
        self.assertEqual(s.arrive("dest_a", age="age_b"), "map_b")
        self.assertEqual(requests(conn), [("age", {"to": "age_b"}), ("wait", {"event": "loading_end"}),
                                          ("teleport", {"to": "dest_a"}), ("wait", {"event": "map", "map": "map_b"}),
                                          ("status", {})])
        self.assertEqual(clock.slept, [8.0])


class SwitchGapTests(unittest.TestCase):
    def script(self):
        return {
            "teleport": [ok(sent=True, map="map_b")],
            "wait": [ok(state="play", waited=5, map="map_b")],
            "status": [status("map_b")],
        }

    def test_a_second_teleport_within_eight_seconds_waits_out_the_rest(self):
        s, conn, clock = session(self.script())
        s.arrive("dest_a")
        clock.now = 3.0
        s.arrive("dest_b")
        self.assertEqual(clock.slept, [5.0])
        self.assertEqual([t for cmd, _, t in conn.sent if cmd == "teleport"], [0.0, 8.0])

    def test_a_teleport_eight_seconds_after_the_last_one_does_not_wait(self):
        s, _, clock = session(self.script())
        s.arrive("dest_a")
        clock.now = 8.0
        s.arrive("dest_b")
        self.assertEqual(clock.slept, [])

    def test_an_age_switch_counts_as_the_last_request(self):
        s, conn, clock = session({
            "age": [ok(age="age_b", sent=True, map="map_c")],
            "wait": [ok(state="play", waited=900, map="map_c")],
            "teleport": [ok(sent=True, map="map_b")],
            "status": [status("map_b")],
        })
        s.age("age_b")
        clock.now = 2.0
        s.arrive("dest_a")
        self.assertEqual(clock.slept, [6.0])
        self.assertEqual([t for cmd, _, t in conn.sent if cmd == "teleport"], [8.0])

    def test_a_teleport_after_an_arrive_that_succeeded_waits_from_the_arrive_ending(self):
        s, conn, _ = session(self.script(), takes={"wait": 20})
        s.arrive("dest_a")
        s.arrive("dest_b")
        self.assertEqual([t for cmd, _, t in conn.sent if cmd == "teleport"], [0.0, 28.0])

    def test_a_teleport_after_an_arrive_that_raised_waits_from_the_arrive_ending(self):
        s, conn, _ = session({
            "teleport": [ok(sent=True, map="map_b")],
            "wait": [failed("timeout", "no map change"), ok(state="play", waited=5, map="map_b")],
            "status": [status("map_b")],
        }, takes={"wait": 30})
        with self.assertRaises(BridgeError):
            s.arrive("dest_a")
        s.arrive("dest_b")
        self.assertEqual([t for cmd, _, t in conn.sent if cmd == "teleport"], [0.0, 38.0])

    def test_a_teleport_after_a_refused_arrive_waits_from_the_refusal_ending(self):
        s, conn, _ = session({
            "teleport": [failed("refused", "an event is playing"), ok(sent=True, map="map_b")],
            "wait": [ok(state="play", waited=5, map="map_b")],
            "status": [status("map_b")],
        }, takes={"teleport": 5})
        with self.assertRaises(BridgeError):
            s.arrive("dest_a")
        s.arrive("dest_b")
        self.assertEqual([t for cmd, _, t in conn.sent if cmd == "teleport"], [0.0, 13.0])

    def test_a_teleport_after_an_age_switch_waits_from_its_load_ending(self):
        s, conn, _ = session({
            "age": [ok(age="age_b", sent=True, map="map_c")],
            "wait": [ok(state="play", waited=900, map="map_c")],
            "teleport": [ok(sent=True, map="map_b")],
            "status": [status("map_b")],
        }, takes={"wait": 20})
        s.age("age_b")
        s.arrive("dest_a")
        self.assertEqual([t for cmd, _, t in conn.sent if cmd == "teleport"], [28.0])

    def test_a_teleport_after_a_refused_age_switch_waits_from_the_refusal_ending(self):
        s, conn, _ = session({
            "age": [failed("refused", "an event is playing")],
            "teleport": [ok(sent=True, map="map_b")],
            "wait": [ok(state="play", waited=5, map="map_b")],
            "status": [status("map_b")],
        }, takes={"age": 5})
        with self.assertRaises(BridgeError):
            s.age("age_b")
        s.arrive("dest_a")
        self.assertEqual([t for cmd, _, t in conn.sent if cmd == "teleport"], [13.0])


class AgeTests(unittest.TestCase):
    def test_a_switch_waits_for_the_load_to_end(self):
        s, conn, _ = session({
            "age": [ok(age="age_b", sent=True, map="map_c")],
            "wait": [ok(state="play", waited=900, map="map_c")],
        })
        s.age("age_b")
        self.assertEqual(requests(conn), [("age", {"to": "age_b"}), ("wait", {"event": "loading_end"})])

    def test_a_switch_to_the_age_already_set_sends_nothing_more(self):
        s, conn, _ = session({"age": [ok(age="age_b", sent=False)]})
        s.age("age_b")
        self.assertEqual(requests(conn), [("age", {"to": "age_b"})])

    def test_a_refused_switch_raises_and_waits_for_nothing(self):
        s, conn, _ = session({"age": [failed("refused", "an event is playing")]})
        with self.assertRaises(BridgeError) as raised:
            s.age("age_b")
        self.assertIn("an event is playing", str(raised.exception))
        self.assertEqual(requests(conn), [("age", {"to": "age_b"})])


class DeviceTests(unittest.TestCase):
    def test_the_expected_renderer_passes(self):
        s, _, _ = session({"status": [status(renderer=RENDERER)]})
        s.check_device(DEVICE)

    def test_another_renderer_raises_naming_both(self):
        s, _, _ = session({"status": [status(renderer="device_b (Vulkan, native)")]})
        with self.assertRaises(BridgeError) as raised:
            s.check_device(DEVICE)
        message = str(raised.exception)
        self.assertIn("device_b (Vulkan, native)", message)
        self.assertIn(RENDERER, message)

    def test_a_renderer_that_is_not_native_vulkan_of_the_device_raises(self):
        for renderer in ("device_a (Vulkan, fallback)", "device_a (OpenGL, native)", "device_a (Vulkan, native) "):
            with self.subTest(renderer=renderer):
                s, _, _ = session({"status": [status(renderer=renderer)]})
                with self.assertRaises(BridgeError):
                    s.check_device(DEVICE)


class WindowTests(unittest.TestCase):
    def test_a_steady_window_returns_its_first_and_last_statuses(self):
        s, conn, _ = session({
            "status": [status(frame=100), status(frame=160), status(frame=220)],
            "wait": [ok(state="play", waited=60)],
        })
        first, last = s.window(120, "map_a")
        self.assertEqual((first["frame"], last["frame"]), (100, 220))
        self.assertEqual(last["replayed"], 219)
        waits = [args for cmd, args in requests(conn) if cmd == "wait"]
        self.assertEqual(waits, [{"event": "present", "frames": 60}] * 2)

    def test_the_window_steps_in_sixties_and_takes_the_rest(self):
        s, conn, _ = session({"status": [status()], "wait": [ok(state="play", waited=60)]})
        s.window(90, "map_a")
        waits = [args for cmd, args in requests(conn) if cmd == "wait"]
        self.assertEqual(waits, [{"event": "present", "frames": 60}, {"event": "present", "frames": 30}])

    def test_a_map_change_inside_the_window_raises_and_stops_there(self):
        s, conn, _ = session({
            "status": [status("map_a"), status("map_b")],
            "wait": [ok(state="play", waited=60)],
        })
        with self.assertRaises(BridgeError) as raised:
            s.window(120, "map_a")
        message = str(raised.exception)
        self.assertIn("map_a", message)
        self.assertIn("map_b", message)
        self.assertEqual([cmd for cmd, _ in requests(conn)].count("wait"), 1)

    def test_a_window_starting_on_another_map_raises(self):
        s, _, _ = session({"status": [status("map_b")], "wait": [ok(state="play", waited=60)]})
        with self.assertRaises(BridgeError):
            s.window(120, "map_a")

    def test_a_load_or_an_event_inside_the_window_raises_unless_allowed(self):
        for case, bad, expect in (("load", status(loading=True), "loading True"),
                                  ("event", status(event=True), "event True")):
            with self.subTest(case=case):
                s, _, _ = session({
                    "status": [status(), bad],
                    "wait": [ok(state="play", waited=60)],
                })
                with self.assertRaises(BridgeError) as raised:
                    s.window(120, "map_a")
                self.assertIn(expect, str(raised.exception))

    def test_an_event_inside_the_window_is_allowed_when_asked(self):
        s, _, _ = session({
            "status": [status(), status(event=True), status(event=True)],
            "wait": [ok(state="play", waited=60)],
        })
        first, last = s.window(120, "map_a", allow_event=True)
        self.assertEqual(last["game"]["event"], True)


class EnterPlayTests(unittest.TestCase):
    def test_a_game_already_in_play_is_not_pressed(self):
        s, conn, _ = session({"status": [status()]})
        self.assertEqual(s.enter_play(3)["state"], "play")
        self.assertEqual(requests(conn), [("status", {})])

    def test_presses_a_and_waits_sixty_presents_until_play_with_no_event_or_load(self):
        s, conn, _ = session({
            "status": [status(state="menu"), status(event=True), status(loading=True), status()],
            "press": [ok(start=1, end=9)],
            "wait": [ok(state="play", waited=60)],
        })
        s.enter_play(3)
        press = ("press", {"buttons": "A"})
        wait = ("wait", {"event": "present", "frames": 60})
        self.assertEqual(requests(conn), [("status", {}), press, wait, ("status", {}), press, wait,
                                          ("status", {}), press, wait, ("status", {})])

    def test_a_game_in_boot_or_intro_is_polled_until_menu_before_the_first_press(self):
        for state in ("boot", "intro"):
            with self.subTest(state=state):
                s, conn, clock = session({
                    "status": [status(state=state), status(state="menu"), status()],
                    "press": [ok(start=1, end=9)],
                    "wait": [ok(state="play", waited=60)],
                })
                s.enter_play(3)
                self.assertEqual(requests(conn), [
                    ("status", {}),
                    ("status", {}),
                    ("press", {"buttons": "A"}),
                    ("wait", {"event": "present", "frames": 60}),
                    ("status", {}),
                ])
                self.assertEqual(clock.slept, [0.25])

    def test_a_game_that_reaches_play_without_menu_is_not_timed_out(self):
        s, conn, clock = session({
            "status": [status(state="boot"), status(state="loading"), status(state="play")],
        })
        self.assertEqual(s.enter_play(3)["state"], "play")
        self.assertEqual(requests(conn), [("status", {}), ("status", {}), ("status", {})])
        self.assertEqual(clock.slept, [0.25, 0.25])

    def test_a_game_in_menu_or_play_is_not_polled_before_the_press(self):
        for state in ("menu", "play"):
            with self.subTest(state=state):
                s, _, clock = session({
                    "status": [status(state=state), status()],
                    "press": [ok(start=1, end=9)],
                    "wait": [ok(state="play", waited=60)],
                })
                s.enter_play(1)
                self.assertEqual(clock.slept, [])

    def test_a_game_that_never_reaches_menu_or_play_raises_naming_its_state_after_the_default_timeout(self):
        s, conn, clock = session({"status": [status(state="boot")]})
        with self.assertRaises(BridgeError) as raised:
            s.enter_play(3)
        self.assertIn("boot", str(raised.exception))
        self.assertEqual(clock.now, 1800.0)
        self.assertNotIn("press", [cmd for cmd, _ in requests(conn)])

    def test_a_timeout_given_to_enter_play_is_the_limit_and_nothing_is_pressed(self):
        s, conn, clock = session({"status": [status(state="intro")]})
        with self.assertRaises(BridgeError) as raised:
            s.enter_play(3, timeout=90)
        self.assertIn("intro", str(raised.exception))
        self.assertEqual(clock.now, 90.0)
        self.assertNotIn("press", [cmd for cmd, _ in requests(conn)])

    def test_raises_after_its_press_limit(self):
        s, conn, _ = session({
            "status": [status(state="menu")],
            "press": [ok(start=1, end=9)],
            "wait": [ok(state="menu", waited=60)],
        })
        with self.assertRaises(BridgeError) as raised:
            s.enter_play(2)
        self.assertIn("2 presses", str(raised.exception))
        self.assertEqual([cmd for cmd, _ in requests(conn)].count("press"), 2)


if __name__ == "__main__":
    unittest.main()
