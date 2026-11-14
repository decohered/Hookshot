"""scripts/harness/bridge.py, the harness's session over one control connection, against a scripted fake connection and a
fake clock. Checks: arrival by teleport, after the game settles (an event playing at the start is waited out before the
teleport is sent, and a settle that does not come raises before it), confirmed by the reported map with no load or event
pending (a refusal, a wrong map, a map that never arrives, and a load or event that never ends each raise, naming what
failed, and nothing is retried); a status counts for a check only when its game.frame is an integer at or after the
frame the check needs, else it is read again every POLL_SECONDS, and one that never gets there raises naming the frames,
while a status without a game.frame never counts; the one request repeated, a teleport the game refuses because an event
is playing or because the game's event manager declined it, sent again after a poll and a settle until the deadline
SETTLE_SECONDS after the first settle began, which raises the refusal when it passes, while any other refusal or error
raises at once; the 8 s gap from the end of a
teleport or age switch, however it ended, to the next, by the clock; an age switch waiting for its load, and an age
already set sending nothing more; the renderer being the expected device; a window that fails on a map change or a load
and returns both its statuses when steady; and entering play: the status polled until menu or play within its timeout,
then the press limit, each press given its paced time by
the clock however fast the game presents. No socket, game, GPU or host is used.

python -I -B tests/test_harness_bridge.py
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "harness"))
from bridge import POLL_SECONDS, SETTLE_SECONDS, BridgeError, Session

DEVICE = "device_a"
RENDERER = "%s (Vulkan, native)" % DEVICE
DECLINED = "the game refused the teleport: the game's event manager declined the request"


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
    reply that is a function is called with the clock's time and answers with what it returns. A command the script
    does not name fails the test. Each request is kept with the clock's time when it was sent, and the clock moves by
    the seconds `takes` gives its command before it is answered."""

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
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        return reply(self.clock()) if callable(reply) else reply


def requests(conn):
    return [(cmd, args) for cmd, args, _ in conn.sent]


def session(script, takes=None):
    clock = FakeClock()
    conn = FakeConn(clock, script, takes)
    return Session(conn, clock=clock, sleep=clock.sleep), conn, clock


def status(map_name="map_a", state="play", event=False, loading=False, renderer=RENDERER, frame=100, game_frame=None):
    """A status reply at frame `frame`, its game.frame the same unless game_frame gives another"""
    game_frame = frame if game_frame is None else game_frame
    return {"ok": True, "frame": frame, "state": state, "map": map_name, "renderer": renderer, "replayed": frame - 1,
            "game": {"event": event, "loading": loading, "age": "age_a", "frame": game_frame}}


def with_game(reply, **fields):
    """The reply with its game object's fields set; a field set to None is removed"""
    game = {key: value for key, value in {**reply["game"], **fields}.items() if value is not None}
    return {**reply, "game": game}


def ok(**fields):
    return {"ok": True, "frame": 100, **fields}


def failed(error, message):
    return {"ok": False, "frame": 100, "error": error, "message": message}


class ArriveTests(unittest.TestCase):
    # A teleport's reply is at its frame, and the teleport takes one present after it: the game is one frame on once it
    # is sent, so the statuses read after a teleport at frame F are at F + 1 here.
    def test_a_teleport_followed_by_the_expected_map_returns_it(self):
        s, conn, _ = session({
            "teleport": [ok(sent=True, map="map_b")],
            "wait": [ok(state="play", waited=187, map="map_b")],
            "status": [status("map_a"), status("map_b", frame=101)],
        })
        self.assertEqual(s.arrive("dest_a"), "map_b")
        self.assertEqual(requests(conn), [("status", {}), ("teleport", {"to": "dest_a"}),
                                          ("wait", {"event": "map", "map": "map_b"}), ("status", {})])

    def test_a_refused_teleport_raises_naming_the_refusal_and_is_not_retried(self):
        s, conn, _ = session({
            "status": [status()],
            "teleport": [failed("refused", "no such destination")],
        })
        with self.assertRaises(BridgeError) as raised:
            s.arrive("dest_a")
        message = str(raised.exception)
        self.assertIn("refused", message)
        self.assertIn("no such destination", message)
        self.assertEqual(requests(conn), [("status", {}), ("teleport", {"to": "dest_a"})])

    def test_a_teleport_refused_for_an_event_waits_a_poll_settles_and_is_sent_again(self):
        s, conn, clock = session({
            "status": [status("map_b"), status("map_b"), status("map_b", frame=101)],
            "teleport": [failed("refused", "an event is playing"), ok(sent=True, map="map_b")],
            "wait": [ok(state="play", waited=5, map="map_b")],
        })
        self.assertEqual(s.arrive("dest_a"), "map_b")
        self.assertEqual(requests(conn), [("status", {}), ("teleport", {"to": "dest_a"}), ("status", {}),
                                          ("teleport", {"to": "dest_a"}), ("wait", {"event": "map", "map": "map_b"}),
                                          ("status", {})])
        self.assertEqual([t for cmd, _, t in conn.sent if cmd == "teleport"], [0.0, POLL_SECONDS])
        self.assertEqual(clock.slept, [POLL_SECONDS])

    def test_an_event_refusal_that_keeps_coming_raises_once_the_deadline_passes(self):
        event_ends = 10.0

        def status_reply(now):
            return status(event=now < event_ends)

        s, conn, clock = session({
            "status": [status_reply],
            "teleport": [failed("refused", "an event is playing")],
        })
        with self.assertRaises(BridgeError) as raised:
            s.arrive("dest_a")
        message = str(raised.exception)
        self.assertIn("refused", message)
        self.assertIn("an event is playing", message)
        teleports = [t for cmd, _, t in conn.sent if cmd == "teleport"]
        self.assertEqual(teleports[0], event_ends)
        self.assertLess(teleports[-1], SETTLE_SECONDS)
        self.assertEqual(clock.now, SETTLE_SECONDS)

    def test_any_other_refusal_or_error_of_a_teleport_raises_at_once(self):
        for error, message in (("refused", "no such destination"), ("timeout", "an event is playing")):
            with self.subTest(error=error, message=message):
                s, conn, clock = session({
                    "status": [status()],
                    "teleport": [failed(error, message)],
                })
                with self.assertRaises(BridgeError) as raised:
                    s.arrive("dest_a")
                self.assertIn(message, str(raised.exception))
                self.assertEqual([cmd for cmd, _ in requests(conn)].count("teleport"), 1)
                self.assertEqual(clock.slept, [])

    def test_a_teleport_declined_by_the_event_manager_is_sent_again_and_arrives(self):
        s, conn, clock = session({
            "status": [status("map_b"), status("map_b"), status("map_b", frame=101)],
            "teleport": [failed("refused", DECLINED), ok(sent=True, map="map_b")],
            "wait": [ok(state="play", waited=5, map="map_b")],
        })
        self.assertEqual(s.arrive("dest_a"), "map_b")
        self.assertEqual(requests(conn), [("status", {}), ("teleport", {"to": "dest_a"}), ("status", {}),
                                          ("teleport", {"to": "dest_a"}), ("wait", {"event": "map", "map": "map_b"}),
                                          ("status", {})])
        self.assertEqual([t for cmd, _, t in conn.sent if cmd == "teleport"], [0.0, POLL_SECONDS])
        self.assertEqual(clock.slept, [POLL_SECONDS])

    def test_a_decline_by_the_event_manager_that_keeps_coming_raises_once_the_deadline_passes(self):
        s, conn, clock = session({
            "status": [status()],
            "teleport": [failed("refused", DECLINED)],
        })
        with self.assertRaises(BridgeError) as raised:
            s.arrive("dest_a")
        message = str(raised.exception)
        self.assertIn("refused", message)
        self.assertIn("the game's event manager declined the request", message)
        teleports = [t for cmd, _, t in conn.sent if cmd == "teleport"]
        self.assertEqual(teleports[0], 0.0)
        self.assertLess(teleports[-1], SETTLE_SECONDS)
        self.assertEqual(clock.now, SETTLE_SECONDS)

    def test_an_event_manager_decline_that_is_not_a_refusal_raises_at_once(self):
        for error in ("timeout", "cancelled"):
            with self.subTest(error=error):
                s, conn, clock = session({
                    "status": [status()],
                    "teleport": [failed(error, DECLINED)],
                })
                with self.assertRaises(BridgeError) as raised:
                    s.arrive("dest_a")
                self.assertIn("event manager declined", str(raised.exception))
                self.assertEqual([cmd for cmd, _ in requests(conn)].count("teleport"), 1)
                self.assertEqual(clock.slept, [])

    def test_a_map_that_never_arrives_raises_naming_the_timeout(self):
        s, _, _ = session({
            "status": [status()],
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
            "status": [status("map_a", frame=101)],
        })
        with self.assertRaises(BridgeError) as raised:
            s.arrive("dest_a")
        message = str(raised.exception)
        self.assertIn("map_a", message)
        self.assertIn("map_b", message)

    def test_a_load_that_never_ends_raises_naming_it(self):
        s, conn, clock = session({
            "teleport": [ok(sent=True, map="map_b")],
            "wait": [ok(state="play", waited=5, map="map_b")],
            "status": [status(), status("map_b", loading=True, frame=101)],
        })
        with self.assertRaises(BridgeError) as raised:
            s.arrive("dest_a")
        self.assertIn("loading True", str(raised.exception))
        self.assertEqual([cmd for cmd, _ in requests(conn)].count("teleport"), 1)
        self.assertGreaterEqual(clock.now, 60)

    def test_an_event_that_never_ends_raises_naming_it(self):
        s, conn, _ = session({
            "teleport": [ok(sent=True, map="map_b")],
            "wait": [ok(state="play", waited=5, map="map_b")],
            "status": [status(), status("map_b", event=True, frame=101)],
        })
        with self.assertRaises(BridgeError) as raised:
            s.arrive("dest_a")
        self.assertIn("event True", str(raised.exception))
        self.assertEqual([cmd for cmd, _ in requests(conn)].count("teleport"), 1)

    def test_an_age_is_switched_before_the_teleport_and_the_gap_applies_after_it(self):
        s, conn, clock = session({
            "age": [ok(age="age_b", sent=True, map="map_c")],
            "wait": [ok(state="play", waited=900, map="map_c"), ok(state="play", waited=5, map="map_b")],
            "teleport": [ok(sent=True, map="map_b")],
            "status": [status("map_b"), status("map_b", frame=101)],
        })
        self.assertEqual(s.arrive("dest_a", age="age_b"), "map_b")
        self.assertEqual(requests(conn), [("age", {"to": "age_b"}), ("wait", {"event": "loading_end"}),
                                          ("status", {}), ("teleport", {"to": "dest_a"}),
                                          ("wait", {"event": "map", "map": "map_b"}), ("status", {})])
        self.assertEqual(clock.slept, [8.0])

    def test_a_settled_game_gets_one_status_read_before_the_teleport_and_no_sleep(self):
        s, conn, clock = session({
            "teleport": [ok(sent=True, map="map_b")],
            "wait": [ok(state="play", waited=5, map="map_b")],
            "status": [status("map_a"), status("map_b", frame=101)],
        })
        self.assertEqual(s.arrive("dest_a"), "map_b")
        self.assertEqual(requests(conn), [("status", {}), ("teleport", {"to": "dest_a"}),
                                          ("wait", {"event": "map", "map": "map_b"}), ("status", {})])
        self.assertEqual(clock.slept, [])

    def test_a_teleport_waits_for_an_event_playing_at_the_start_to_end_and_is_refused_before(self):
        event_ends = 10.0
        game = {"map": "map_a"}
        presents = {"frame": 100}

        def status_reply(now):
            return status(game["map"], event=now < event_ends, frame=presents["frame"])

        def teleport_reply(now):
            if now < event_ends:
                return failed("refused", "an event is playing")
            game["map"] = "map_b"
            presents["frame"] += 1   # the teleport takes one present
            return ok(sent=True, map="map_b")

        s, conn, _ = session({
            "status": [status_reply],
            "teleport": [teleport_reply],
            "wait": [ok(state="play", waited=5, map="map_b")],
        })
        self.assertEqual(s.arrive("dest_a"), "map_b")
        self.assertEqual([t for cmd, _, t in conn.sent if cmd == "teleport"], [event_ends])

    def test_an_event_playing_at_the_start_that_never_ends_raises_before_any_teleport(self):
        s, conn, clock = session({
            "status": [status(event=True)],
            "teleport": [ok(sent=True, map="map_b")],
        })
        with self.assertRaises(BridgeError) as raised:
            s.arrive("dest_a")
        self.assertIn("event True", str(raised.exception))
        self.assertNotIn("teleport", [cmd for cmd, _ in requests(conn)])
        self.assertEqual(clock.now, 60.0)

    def test_a_settled_status_at_the_teleport_frame_is_not_the_arrival_until_a_later_frame(self):
        s, conn, clock = session({
            "status": [status("map_a"), status("map_b", frame=100), status("map_b", frame=101)],
            "teleport": [ok(sent=True, map="map_b", frame=100)],
            "wait": [ok(state="play", waited=5, map="map_b")],
        })
        self.assertEqual(s.arrive("dest_a"), "map_b")
        self.assertEqual([cmd for cmd, _ in requests(conn)], ["status", "teleport", "wait", "status", "status"])
        self.assertEqual(clock.slept, [POLL_SECONDS])


class SwitchGapTests(unittest.TestCase):
    def script(self):
        # two arrivals: the first teleport is sent at frame 100 and the second at 101, each taking one present
        return {
            "teleport": [ok(sent=True, map="map_b", frame=100), ok(sent=True, map="map_b", frame=101)],
            "wait": [ok(state="play", waited=5, map="map_b")],
            "status": [status("map_b", frame=100), status("map_b", frame=101), status("map_b", frame=101),
                       status("map_b", frame=102)],
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
            "status": [status("map_b"), status("map_b", frame=101)],
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
            "teleport": [ok(sent=True, map="map_b", frame=100), ok(sent=True, map="map_b", frame=101)],
            "wait": [failed("timeout", "no map change"), ok(state="play", waited=5, map="map_b")],
            "status": [status("map_b", frame=100), status("map_b", frame=101), status("map_b", frame=102)],
        }, takes={"wait": 30})
        with self.assertRaises(BridgeError):
            s.arrive("dest_a")
        s.arrive("dest_b")
        self.assertEqual([t for cmd, _, t in conn.sent if cmd == "teleport"], [0.0, 38.0])

    def test_a_teleport_after_a_refused_arrive_waits_from_the_refusal_ending(self):
        s, conn, _ = session({
            "teleport": [failed("refused", "no such destination"), ok(sent=True, map="map_b", frame=100)],
            "wait": [ok(state="play", waited=5, map="map_b")],
            "status": [status("map_b", frame=100), status("map_b", frame=100), status("map_b", frame=101)],
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
            "status": [status("map_b"), status("map_b", frame=101)],
        }, takes={"wait": 20})
        s.age("age_b")
        s.arrive("dest_a")
        self.assertEqual([t for cmd, _, t in conn.sent if cmd == "teleport"], [28.0])

    def test_a_teleport_after_a_refused_age_switch_waits_from_the_refusal_ending(self):
        s, conn, _ = session({
            "age": [failed("refused", "an event is playing")],
            "teleport": [ok(sent=True, map="map_b")],
            "wait": [ok(state="play", waited=5, map="map_b")],
            "status": [status("map_b"), status("map_b", frame=101)],
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

    def test_a_switch_declined_by_the_event_manager_raises_and_is_sent_once(self):
        reason = "the game refused the age switch: the game's event manager declined the request"
        s, conn, _ = session({"age": [failed("refused", reason)]})
        with self.assertRaises(BridgeError) as raised:
            s.age("age_b")
        self.assertIn("the game's event manager declined the request", str(raised.exception))
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
            "wait": [ok(state="play", waited=60, frame=160), ok(state="play", waited=60, frame=220)],
        })
        first, last = s.window(120, "map_a")
        self.assertEqual((first["frame"], last["frame"]), (100, 220))
        self.assertEqual(last["replayed"], 219)
        waits = [args for cmd, args in requests(conn) if cmd == "wait"]
        self.assertEqual(waits, [{"event": "present", "frames": 60}] * 2)

    def test_the_window_steps_in_sixties_and_takes_the_rest(self):
        s, conn, _ = session({
            "status": [status(frame=100), status(frame=160), status(frame=190)],
            "wait": [ok(state="play", waited=60, frame=160), ok(state="play", waited=30, frame=190)],
        })
        s.window(90, "map_a")
        waits = [args for cmd, args in requests(conn) if cmd == "wait"]
        self.assertEqual(waits, [{"event": "present", "frames": 60}, {"event": "present", "frames": 30}])

    def test_a_map_change_inside_the_window_raises_and_stops_there(self):
        s, conn, _ = session({
            "status": [status("map_a"), status("map_b", frame=160)],
            "wait": [ok(state="play", waited=60, frame=160)],
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
        for case, bad, expect in (("load", status(loading=True, frame=160), "loading True"),
                                  ("event", status(event=True, frame=160), "event True")):
            with self.subTest(case=case):
                s, _, _ = session({
                    "status": [status(), bad],
                    "wait": [ok(state="play", waited=60, frame=160)],
                })
                with self.assertRaises(BridgeError) as raised:
                    s.window(120, "map_a")
                self.assertIn(expect, str(raised.exception))

    def test_an_event_inside_the_window_is_allowed_when_asked(self):
        s, _, _ = session({
            "status": [status(), status(event=True, frame=160), status(event=True, frame=220)],
            "wait": [ok(state="play", waited=60, frame=160), ok(state="play", waited=60, frame=220)],
        })
        first, last = s.window(120, "map_a", allow_event=True)
        self.assertEqual(last["game"]["event"], True)


class FreshnessTests(unittest.TestCase):
    def test_settle_rereads_a_settled_status_whose_game_frame_is_behind_its_frame(self):
        s, conn, clock = session({"status": [status(frame=100, game_frame=99), status(frame=100)]})
        self.assertEqual(s.settle(SETTLE_SECONDS)["game"]["frame"], 100)
        self.assertEqual(requests(conn), [("status", {}), ("status", {})])
        self.assertEqual(clock.slept, [POLL_SECONDS])

    def test_settle_reads_at_the_frame_its_caller_passes_over_its_first_status_frame(self):
        s, conn, _ = session({"status": [status(frame=100), status(frame=105)]})
        self.assertEqual(s.settle(SETTLE_SECONDS, frame=105)["frame"], 105)
        self.assertEqual(requests(conn), [("status", {}), ("status", {})])

    def test_a_status_that_never_reaches_its_frame_raises_naming_both_after_settle_seconds(self):
        s, _, clock = session({"status": [status(frame=100, game_frame=90)]})
        with self.assertRaises(BridgeError) as raised:
            s.settle(SETTLE_SECONDS)
        message = str(raised.exception)
        self.assertIn("at frame 100", message)
        self.assertIn("game.frame 90", message)
        self.assertEqual(clock.now, SETTLE_SECONDS)

    def test_a_status_without_an_integer_game_frame_is_never_fresh(self):
        for case, game_frame in (("missing", None), ("a string", "100"), ("a float", 100.0), ("a bool", True)):
            with self.subTest(case=case):
                s, _, clock = session({"status": [with_game(status(), frame=game_frame)]})
                with self.assertRaises(BridgeError):
                    s.settle(SETTLE_SECONDS)
                self.assertEqual(clock.now, SETTLE_SECONDS)
                s, _, clock = session({"status": [with_game(status(), frame=game_frame)]})
                with self.assertRaises(BridgeError):
                    s.window(120, "map_a")
                self.assertEqual(clock.now, SETTLE_SECONDS)

    def test_a_window_rereads_a_stale_steady_status_and_fails_on_the_fresh_event(self):
        s, conn, _ = session({"status": [status(game_frame=99), status(event=True)]})
        with self.assertRaises(BridgeError) as raised:
            s.window(120, "map_a")
        self.assertIn("event True", str(raised.exception))
        self.assertEqual(requests(conn), [("status", {}), ("status", {})])

    def test_a_window_status_after_a_wait_is_read_again_until_its_game_frame_reaches_the_wait_frame(self):
        s, conn, _ = session({
            "status": [status(frame=100), status(frame=160, game_frame=150), status(frame=160)],
            "wait": [ok(state="play", waited=60, frame=160)],
        })
        s.window(60, "map_a")
        self.assertEqual(requests(conn), [("status", {}), ("wait", {"event": "present", "frames": 60}),
                                          ("status", {}), ("status", {})])

    def test_a_window_status_that_never_reaches_the_wait_frame_raises_naming_both_after_settle_seconds(self):
        s, _, clock = session({
            "status": [status(frame=100), status(frame=160, game_frame=150)],
            "wait": [ok(state="play", waited=60, frame=160)],
        })
        with self.assertRaises(BridgeError) as raised:
            s.window(120, "map_a")
        message = str(raised.exception)
        self.assertIn("frame 160", message)
        self.assertIn("game.frame 150", message)
        self.assertEqual(clock.now, SETTLE_SECONDS)


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

    def test_presents_ten_times_faster_than_60_hz_still_give_each_press_its_paced_time(self):
        def game(now):
            # the save loads until 20 s of paced time have passed, fewer than the 30 presses' 30 s
            return status(state="loading", loading=True) if now < 20 else status()

        s, conn, _ = session({
            "status": [status(state="menu"), game],
            "press": [ok(start=1, end=9)],
            "wait": [ok(state="play", waited=60)],
        }, takes={"wait": 0.1})
        self.assertEqual(s.enter_play(30)["state"], "play")
        presses = [t for cmd, _, t in conn.sent if cmd == "press"]
        self.assertLess(len(presses), 30)
        for before, after in zip(presses, presses[1:]):
            self.assertAlmostEqual(after - before, 1.0)

    def test_a_paced_game_gets_no_extra_time_and_its_requests_are_unchanged(self):
        s, conn, clock = session({
            "status": [status(state="menu"), status(state="loading", loading=True), status()],
            "press": [ok(start=1, end=9)],
            "wait": [ok(state="play", waited=60)],
        }, takes={"wait": 1.0})
        s.enter_play(3)
        press = ("press", {"buttons": "A"})
        wait = ("wait", {"event": "present", "frames": 60})
        self.assertEqual(requests(conn), [("status", {}), press, wait, ("status", {}), press, wait, ("status", {})])
        self.assertEqual(clock.slept, [])

    def test_a_game_in_boot_or_intro_is_polled_until_menu_before_the_first_press(self):
        for state in ("boot", "intro"):
            with self.subTest(state=state):
                s, conn, clock = session({
                    "status": [status(state=state), status(state="menu"), status()],
                    "press": [ok(start=1, end=9)],
                    "wait": [ok(state="play", waited=60)],
                }, takes={"wait": 1.0})
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
                }, takes={"wait": 1.0})
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
