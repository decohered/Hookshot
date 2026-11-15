"""The harness's session with a running game over one control connection (the bridge, docs/control.md). A session enters
play from a save, switches age, arrives somewhere by teleport, waits for the teleport's arrival event to begin and to end,
and confirms it by the map the game reports with no load or event pending, checks that the renderer is the expected
device, and holds a window that fails on a map change or a load. A request the game refuses, or a state that does not
come, raises BridgeError naming it. The one request sent again is a teleport the game refuses because an event is
playing or because its event manager declined it: Session.arrive settles and sends it again until its deadline. The
game's state is acted on only from a status whose game.frame, the present count its flags were read at, is an integer
at or after the frame the check needs (_fresh): a status that is not is read again.

The connection is any object with request(cmd, timeout=None, **args) that returns the reply as a dict, as
hookshot_ctl.Bridge does. The clock and the sleep are injected, so the tests run without waiting. Stdlib only.
"""
import time

SWITCH_GAP = 8        # seconds the game refuses a teleport or an age switch after the last one
ENTER_FRAMES = 60     # presents waited after each press when entering play
PRESENT_HZ = 60       # the game's paced present rate (host/nn_vi.c)
PRESS_SECONDS = ENTER_FRAMES / PRESENT_HZ   # the wall-clock time ENTER_FRAMES presents take at that rate: a press's least
WINDOW_STEP = 60      # game frames per step of a window
SETTLE_SECONDS = 60   # how long arrive waits for play with no event or load; the deadline for resending its teleport
POLL_SECONDS = 0.25   # between the status reads of a settle, and before resending a teleport refused for an event
ENTER_SECONDS = 1800  # enter_play's limit for menu or play: a fresh build takes minutes to prepare its shaders


class BridgeError(Exception):
    """A request the game refused or failed, or a state the session did not reach: the message names it"""


def _failure(cmd, reply):
    return "%s failed: %s: %s" % (cmd, reply.get("error"), reply.get("message"))


def _event_refusal(reply):
    """True when the game refused the request because an event is playing, or because its event manager declined it"""
    message = reply.get("message") or ""
    return reply.get("error") == "refused" and any(
        reason in message for reason in ("an event is playing", "the game's event manager declined the request"))


def _settled(status):
    game = status.get("game") or {}
    return status.get("state") == "play" and game.get("event") is False and game.get("loading") is False


def _game_frame(status):
    return (status.get("game") or {}).get("frame")


def _fresh(status, needed):
    """True when the status's game.frame is an integer at or after `needed`: only a status read at that frame or later
    shows the game's state there. A bool is not a frame."""
    frame = _game_frame(status)
    return type(frame) is int and frame >= needed


def _begun(status, expected, needed):
    """True when the status is read at or after `needed` on the reported map with the arrival event playing"""
    return _fresh(status, needed) and status.get("map") == expected and (status.get("game") or {}).get("event") is True


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
    raise BridgeError("the window is not steady at game.frame %s: %s" % (_game_frame(status), bad))


def _wrong_map(to, expected, status):
    return BridgeError("teleport to %r arrived on %r, not the reported %r" % (to, status.get("map"), expected))


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
            raise BridgeError(_failure(cmd, reply))
        return reply

    def status(self):
        return self.call("status")

    def enter_play(self, presses, timeout=ENTER_SECONDS):
        """Reads the status until the game is in menu or play, for up to `timeout` s by the clock, then presses A up to
        `presses` times, waiting ENTER_FRAMES presents after each and, by the clock, until PRESS_SECONDS have passed since
        the press, so a game presenting faster than PRESENT_HZ gives each press its paced time. It stops once the game is
        in play with no event or load, and returns that status. A game already there is not pressed. Raises BridgeError
        naming the state when the game is in neither menu nor play by the deadline, and when it is not in play after the
        last press."""
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
            started = self.clock()
            self.call("press", buttons="A")
            self.call("wait", event="present", frames=ENTER_FRAMES)
            left = started + PRESS_SECONDS - self.clock()
            if left > 0:
                self.sleep(left)
            status = self.status()
        if _settled(status):
            return status
        raise BridgeError("not in play after %d presses: %s" % (presses, _describe(status)))

    def settle(self, timeout_s):
        """Reads the status until the game is in play with no event or load, read at or after the frame of its first
        status, and returns it. Raises BridgeError, naming the state and the frame needed, when that has not come
        within timeout_s by the clock."""
        deadline = self.clock() + timeout_s
        status = self.status()
        needed = status["frame"]
        while not (_settled(status) and _fresh(status, needed)):
            if self.clock() >= deadline:
                raise BridgeError("not in play with no event or load at frame %s after %s s: %s, game.frame %s"
                                  % (needed, timeout_s, _describe(status), _game_frame(status)))
            self.sleep(POLL_SECONDS)
            status = self.status()
        return status

    def _fresh_status(self, status, needed=None):
        """Returns `status` once it is read at or after `needed` (its own frame when none is given). While it is not,
        the game is presented one frame and the status read again, for up to SETTLE_SECONDS by the clock. It never
        sleeps, so the frames it waits for pass in the game. Raises BridgeError, naming the frame needed and the last
        game.frame, when that does not come."""
        needed = status["frame"] if needed is None else needed
        deadline = self.clock() + SETTLE_SECONDS
        while not _fresh(status, needed):
            if self.clock() >= deadline:
                raise BridgeError("the window's status is not at frame %s after %s s: last game.frame %s"
                                  % (needed, SETTLE_SECONDS, _game_frame(status)))
            self.call("wait", event="present", frames=1)
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

    def _teleport(self, to):
        """Settles, then sends the teleport to `to` and returns its reply. The game refuses a teleport while an event
        plays, and the status a settle reads can be about 350 ms old, so this is the one request sent again: a teleport
        refused because an event is playing, or because its event manager declined it, is followed by POLL_SECONDS, a
        settle and the teleport again, for as long as the deadline SETTLE_SECONDS after the settle began has not passed.
        A refusal that comes once it has passed raises, and so does any other refusal or error, at once. No switch
        gap is waited between the sends, since the game starts none for a refused teleport."""
        deadline = self.clock() + SETTLE_SECONDS
        self.settle(SETTLE_SECONDS)
        reply = self.conn.request("teleport", to=to)
        while _event_refusal(reply) and self.clock() < deadline:
            self.sleep(POLL_SECONDS)
            if self.clock() < deadline:
                self.settle(deadline - self.clock())
                reply = self.conn.request("teleport", to=to)
        if not reply.get("ok"):
            raise BridgeError(_failure("teleport", reply))
        return reply

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

    def _arrival_begins(self, to, expected, needed):
        """Returns the status at which the teleport's arrival event begins: the status is read once, then after each
        one-frame present while it has not begun, for up to SETTLE_SECONDS by the clock. A status read at or after
        `needed` on another map raises the wrong-map error at once, and one read before `needed` is not counted. Raises
        BridgeError, naming the arrival event, the map and the last status, when the event does not begin in time."""
        deadline = self.clock() + SETTLE_SECONDS
        status = self.status()
        while not _begun(status, expected, needed):
            if _fresh(status, needed) and status.get("map") != expected:
                raise _wrong_map(to, expected, status)
            if self.clock() >= deadline:
                raise BridgeError("the arrival event of the teleport to %r did not begin on %r at frame %s or later "
                                  "within %s s: last status %s, map %r, game.frame %s"
                                  % (to, expected, needed, SETTLE_SECONDS, _describe(status), status.get("map"),
                                     _game_frame(status)))
            self.call("wait", event="present", frames=1)
            status = self.status()
        return status

    def _arrival_ends(self, needed):
        """Returns the status at which the teleport's arrival event has ended: the status is read once, then after each
        one-frame present while it is not in play with no event or load at a frame `needed` or later, for up to
        SETTLE_SECONDS by the clock, as _arrival_begins reads the event's begin. Raises BridgeError, naming the frame
        needed and the last status, when that does not come in time."""
        deadline = self.clock() + SETTLE_SECONDS
        status = self.status()
        while not (_settled(status) and _fresh(status, needed)):
            if self.clock() >= deadline:
                raise BridgeError("not in play with no event or load at frame %s after %s s: %s, game.frame %s"
                                  % (needed, SETTLE_SECONDS, _describe(status), _game_frame(status)))
            self.call("wait", event="present", frames=1)
            status = self.status()
        return status

    def arrive(self, to, age=None):
        """Switches to `age` if one is given, then, once the switch gap has passed, waits until the game is in play with
        no event or load, teleports to `to`, and waits for the teleport's arrival event. Once the game reports the map,
        the event must begin: a status read at or after the teleport's frame + 1 on that map with the event playing,
        read after each one-frame present, within SETTLE_SECONDS. Then the event must end: a status read at or after the
        begin's frame + 1 in play with no event or load, read after each one-frame present as the begin is
        (_arrival_ends), within SETTLE_SECONDS. So the return comes at the status of the event's end frame, not at a
        POLL_SECONDS poll. Returns the map the game reported for the teleport, once the game is on it and the event has
        ended. The teleport is the one request repeated: when the game refuses it because an event is playing, or
        because its event manager declined it, it is sent again after a settle, until SETTLE_SECONDS have passed since
        the first settle began (see _teleport). Raises BridgeError when the game does not settle before the teleport,
        when it refuses the teleport for another reason or fails it, when the map does not come, when the arrival
        event does not begin or does not end, or when the game is on another map. The gap for the next switch counts
        from when this returns or raises."""
        if age is not None:
            self.age(age)
        self._wait_gap()
        try:
            reply = self._teleport(to)
            expected = reply.get("map")
            if not expected:
                raise BridgeError("teleport to %r reported no map" % (to,))
            self.call("wait", event="map", map=expected)
            # strictly after the teleport's frame: the teleport takes one present after its reply, so the first whole
            # frame it can show is the reply's frame + 1
            begun = self._arrival_begins(to, expected, reply["frame"] + 1)
            status = self._arrival_ends(begun["game"]["frame"] + 1)
            if status.get("map") != expected:
                raise _wrong_map(to, expected, status)
            return expected
        finally:
            self.last_switch = self.clock()

    def window(self, frames, expect_map, allow_event=False):
        """Holds the game for `frames` game frames, in steps of WINDOW_STEP, and reads the status before the first step
        and after each: the map must stay expect_map with no load, and with no event unless allow_event. Each step waits
        for the game frame that the first status's game.frame plus the frames held so far gives, so the window ends at
        first + frames whatever the frame rate, and a wait that overshoots does not move the next step. Each status
        after a step is read at or after the step's frame (_fresh_status). Returns the first status and the last. Raises
        BridgeError naming what changed."""
        first = self._fresh_status(self.status())
        _check_steady(first, expect_map, allow_event)
        start = first["game"]["frame"]
        last = first
        held = 0
        while held < frames:
            held += min(WINDOW_STEP, frames - held)
            self.call("wait", event="frame", frame=start + held)
            last = self._fresh_status(self.status(), start + held)
            _check_steady(last, expect_map, allow_event)
        return first, last
