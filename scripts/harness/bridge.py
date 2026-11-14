"""The harness's session with a running game over one control connection (the bridge, docs/control.md). A session enters
play from a save, switches age, arrives somewhere by teleport and confirms it by the map the game reports, with no load
or event pending, checks that the renderer is the expected device, and holds a window that fails on a map change or a
load. A request the game refuses, or a state that does not come, raises BridgeError naming it; nothing is retried.

The connection is any object with request(cmd, timeout=None, **args) that returns the reply as a dict, as
hookshot_ctl.Bridge does. The clock and the sleep are injected, so the tests run without waiting. Stdlib only.
"""
import time

SWITCH_GAP = 8        # seconds the game refuses a teleport or an age switch after the last one
ENTER_FRAMES = 60     # presents waited after each press when entering play
WINDOW_STEP = 60      # presents per step of a window
SETTLE_SECONDS = 60   # how long arrive waits for play with no event or load
POLL_SECONDS = 0.25   # between the status reads of a settle
ENTER_SECONDS = 1800  # enter_play's limit for menu or play: a fresh build takes minutes to prepare its shaders


class BridgeError(Exception):
    """A request the game refused or failed, or a state the session did not reach: the message names it"""


def _settled(status):
    game = status.get("game") or {}
    return status.get("state") == "play" and game.get("event") is False and game.get("loading") is False


def _at_menu_or_play(status):
    return status.get("state") in ("menu", "play")


def _describe(status):
    game = status.get("game") or {}
    return "state %s, event %s, loading %s" % (status.get("state"), game.get("event"), game.get("loading"))


def _check_steady(status, expect_map, allow_event):
    """Raises BridgeError naming what is not steady at this status of a window: the map is expect_map, no load is going
    on, and no event is playing unless allow_event"""
    game = status.get("game") or {}
    if status.get("map") != expect_map:
        bad = "map %r, not %r" % (status.get("map"), expect_map)
    elif game.get("loading") is not False:
        bad = "loading %s" % game.get("loading")
    elif not allow_event and game.get("event") is not False:
        bad = "event %s" % game.get("event")
    else:
        return
    raise BridgeError("the window is not steady at frame %s: %s" % (status.get("frame"), bad))


class Session:
    """One game on one connection. Each method sends what it needs, waits where the game says to, and stops at the first
    request the game refuses."""

    def __init__(self, conn, clock=time.monotonic, sleep=time.sleep):
        self.conn = conn
        self.clock = clock
        self.sleep = sleep
        self.last_switch = None

    def call(self, cmd, **args):
        """The game's reply to one request. A reply with ok false raises BridgeError with its error and its message"""
        reply = self.conn.request(cmd, **args)
        if not reply.get("ok"):
            raise BridgeError("%s failed: %s: %s" % (cmd, reply.get("error"), reply.get("message")))
        return reply

    def status(self):
        return self.call("status")

    def enter_play(self, presses, timeout=ENTER_SECONDS):
        """Reads the status until the game is in menu or play, for up to `timeout` s by the clock, then presses A up to
        `presses` times, waiting ENTER_FRAMES presents after each, until the game is in play with no event or load, and
        returns that status. A game already there is not pressed. Raises BridgeError naming the state when the game is
        in neither menu nor play by the deadline, and when it is not in play after the last press."""
        status = self.status()
        deadline = self.clock() + timeout
        while not _at_menu_or_play(status):
            if self.clock() >= deadline:
                raise BridgeError("not in menu or play after %s s: %s" % (timeout, _describe(status)))
            self.sleep(POLL_SECONDS)
            status = self.status()
        for _ in range(presses):
            if _settled(status):
                return status
            self.call("press", buttons="A")
            self.call("wait", event="present", frames=ENTER_FRAMES)
            status = self.status()
        if _settled(status):
            return status
        raise BridgeError("not in play after %d presses: %s" % (presses, _describe(status)))

    def settle(self, timeout_s):
        """Reads the status until the game is in play with no event or load, and returns it. Raises BridgeError, naming
        the state, when that has not come within timeout_s by the clock."""
        deadline = self.clock() + timeout_s
        status = self.status()
        while not _settled(status):
            if self.clock() >= deadline:
                raise BridgeError("not in play with no event or load after %s s: %s" % (timeout_s, _describe(status)))
            self.sleep(POLL_SECONDS)
            status = self.status()
        return status

    def check_device(self, expect):
        """Raises BridgeError, naming both renderers, unless the game renders on `expect`, on Vulkan, natively"""
        want = "%s (Vulkan, native)" % expect
        renderer = self.status().get("renderer")
        if renderer != want:
            raise BridgeError("the renderer is %r, not %r" % (renderer, want))

    def _wait_gap(self):
        """Sleeps until SWITCH_GAP seconds have passed by the clock since this session's last teleport or age switch
        ended"""
        if self.last_switch is not None:
            left = self.last_switch + SWITCH_GAP - self.clock()
            if left > 0:
                self.sleep(left)

    def age(self, to):
        """Switches the player's age to `to` and waits for the load to end. An age the player already has sends nothing
        more. Returns the reply. The gap for the next switch counts from when this one ends, however it ends."""
        self._wait_gap()
        try:
            reply = self.call("age", to=to)
            if reply.get("sent"):
                self.call("wait", event="loading_end")
        finally:
            self.last_switch = self.clock()
        return reply

    def arrive(self, to, age=None):
        """Switches to `age` if one is given, teleports to `to`, and waits until the game is in play with no event or
        load. Returns the map the game reported for the teleport, once the game is on it. Raises BridgeError when the
        game refuses the teleport, when the map does not come, or when the game is on another map. The gap for the next
        switch counts from when this returns or raises."""
        if age is not None:
            self.age(age)
        self._wait_gap()
        try:
            reply = self.call("teleport", to=to)
            expected = reply.get("map")
            if not expected:
                raise BridgeError("teleport to %r reported no map" % (to,))
            self.call("wait", event="map", map=expected)
            status = self.settle(SETTLE_SECONDS)
            if status.get("map") != expected:
                raise BridgeError("teleport to %r arrived on %r, not the reported %r"
                                  % (to, status.get("map"), expected))
            return expected
        finally:
            self.last_switch = self.clock()

    def window(self, frames, expect_map, allow_event=False):
        """Holds the game for `frames` presents, in steps of WINDOW_STEP, and reads the status before the first step and
        after each: the map must stay expect_map with no load, and with no event unless allow_event. Returns the first
        status and the last. Raises BridgeError naming what changed."""
        first = self.status()
        _check_steady(first, expect_map, allow_event)
        last = first
        held = 0
        while held < frames:
            step = min(WINDOW_STEP, frames - held)
            self.call("wait", event="present", frames=step)
            held += step
            last = self.status()
            _check_steady(last, expect_map, allow_event)
        return first, last
