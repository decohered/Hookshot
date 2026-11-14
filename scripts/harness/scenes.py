"""The harness's scene suite: suite.toml in the scenes folder under the harness root, which lives outside git. load() reads
the file, validates it and returns its scenes with the sha256 of its bytes, which each run records. A suite that lacks a
kind, a cache, a motion or a held-out scene, or a scene that misdescribes itself, raises SceneError naming the file and
the key. select() returns a scene by name, and a held-out scene only when final is set. Stdlib only.

    [method]                 optional: any keys, read by harness-verdicts and not checked here; the sha256 covers it
    [scenes.<name>]          name: letters, digits, _ or -
    kind = "light"           light, medium, dense, load, cutscene or soak
    cache = "warm"           warm or cold
    motion = "still"         still or moving
    save = "<folder>"        the folder of the scene's save under scenes/saves
    slot = "01"              optional: two digits
    hour = 12                optional: 0 to 23
    route = [{ teleport = 0 }, { age = "child" }, { inputs = "<script>" }, { wait = 60 }]
    load_to = 0              required for kind load, absent otherwise: an index or a name
    inputs = "<script>"      required when motion is moving: a bridge sequence script
    seconds = 120            1800 to 3600 for soak, at least 1 otherwise
    held_out = false         optional, default false
"""
import hashlib
import json
import posixpath
import re
import tomllib
from dataclasses import dataclass

KINDS = ("light", "medium", "dense", "load", "cutscene", "soak")
CACHES = ("warm", "cold")
MOTIONS = ("still", "moving")
AGES = ("child", "adult")
STEPS = ("teleport", "age", "inputs", "wait")
SCENE_KEYS = ("kind", "cache", "motion", "save", "slot", "hour", "route", "load_to", "inputs", "seconds", "held_out")
COVERED = (("kind", KINDS), ("cache", CACHES), ("motion", MOTIONS), ("held_out", (True, False)))
SOAK_SECONDS = (1800, 3600)
NAME = re.compile(r"[A-Za-z0-9_-]+")
SLOT = re.compile(r"[0-9]{2}")


class SceneError(Exception):
    pass


@dataclass(frozen=True)
class Scene:
    name: str
    kind: str
    cache: str
    motion: str
    save: str
    slot: str | None
    hour: int | None
    route: tuple[dict[str, object], ...]
    load_to: int | str | None
    inputs: str | None
    seconds: int
    held_out: bool


@dataclass(frozen=True)
class Suite:
    path: str
    sha256: str
    scenes: dict[str, Scene]


def load(path):
    """The suite in the file at path, validated: SceneError if it cannot be read or is not a valid suite"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise SceneError(f"{path}: cannot read it: {e.strerror}") from None
    return parse(data, str(path))


def parse(data, where):
    """The suite in data, the bytes of the file named where: validated, with the sha256 of data"""
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise SceneError(f"{where}: not UTF-8 text") from None
    except tomllib.TOMLDecodeError as e:
        raise SceneError(f"{where}: not valid TOML: {e}") from None
    _known(where, "", doc, ("scenes", "method"))
    tables = _need(where, "", doc, "scenes")
    if not isinstance(tables, dict):
        raise SceneError(f"{where}: scenes must be a table of [scenes.<name>] tables")
    if "method" in doc and not isinstance(doc["method"], dict):
        raise SceneError(f"{where}: method must be a table")
    parsed = {name: _scene(where, name, table) for name, table in tables.items()}
    _coverage(where, parsed)
    return Suite(path=where, sha256=hashlib.sha256(data).hexdigest(), scenes=parsed)


def select(suite, name, final):
    """The scene called name. A held-out scene is returned only when final is set."""
    scene = suite.scenes.get(name)
    if scene is None:
        raise SceneError(f"{suite.path}: no scene named {name!r}")
    if scene.held_out and not final:
        raise SceneError(f"{suite.path}: scene {name!r} is held out and runs only in a final run")
    return scene


def scene_dir(root):
    """The scenes folder under the harness root"""
    return posixpath.join(root, "scenes")


def save_dir(root, save):
    """The folder of one scene's save"""
    return posixpath.join(scene_dir(root), "saves", save)


def _scene(where, name, table):
    if not NAME.fullmatch(name):
        raise SceneError(f"{where}: scene name {name!r} must be letters, digits, _ or -")
    if not isinstance(table, dict):
        raise SceneError(f"{where}: scenes.{name} must be a table")
    at = f"scenes.{name}."
    _known(where, at, table, SCENE_KEYS)
    kind = _choice(where, at, table, "kind", KINDS)
    cache = _choice(where, at, table, "cache", CACHES)
    motion = _choice(where, at, table, "motion", MOTIONS)
    save = _match(where, at, table, "save", NAME, "a folder name: letters, digits, _ or -")
    slot = _match(where, at, table, "slot", SLOT, "two digits") if "slot" in table else None
    hour = _whole(where, at, table, "hour", 0, 23) if "hour" in table else None
    route = _route(where, at, table)
    load_to = None
    if kind == "load":
        load_to = _destination(where, at, table, "load_to")
    elif "load_to" in table:
        raise SceneError(f"{where}: {at}load_to is only for kind load")
    inputs = None
    if motion == "moving" or "inputs" in table:
        inputs = _text(where, at, table, "inputs")
    seconds = _whole(where, at, table, "seconds", 1)
    if kind == "soak" and not SOAK_SECONDS[0] <= seconds <= SOAK_SECONDS[1]:
        raise SceneError(f"{where}: {at}seconds must be {SOAK_SECONDS[0]} to {SOAK_SECONDS[1]} for kind soak: {seconds}")
    held_out = table.get("held_out", False)
    if not isinstance(held_out, bool):
        raise SceneError(f"{where}: {at}held_out must be true or false")
    return Scene(name=name, kind=kind, cache=cache, motion=motion, save=save, slot=slot, hour=hour, route=route,
                 load_to=load_to, inputs=inputs, seconds=seconds, held_out=held_out)


def _route(where, at, table):
    steps = _need(where, at, table, "route")
    if not isinstance(steps, list):
        raise SceneError(f"{where}: {at}route must be a list of steps")
    return tuple(_step(where, f"{at}route[{i}]", step) for i, step in enumerate(steps))


def _step(where, label, step):
    """One route step: a table with exactly one of STEPS' keys, its value checked for that key"""
    if not isinstance(step, dict) or len(step) != 1:
        raise SceneError(f"{where}: {label} must be a table with exactly one of {', '.join(STEPS)}")
    (key,) = step
    at = label + "."
    if key == "teleport":
        _destination(where, at, step, key)
    elif key == "age":
        _choice(where, at, step, key, AGES)
    elif key == "inputs":
        _text(where, at, step, key)
    elif key == "wait":
        _whole(where, at, step, key, 1)
    else:
        raise SceneError(f"{where}: unknown key {at}{key}")
    return step


def _coverage(where, parsed):
    """The suite's own requirements: a scene of each kind, of each cache and of each motion, and a held-out scene and
    one that is not"""
    have = {(attr, getattr(scene, attr)) for scene in parsed.values() for attr, _values in COVERED}
    for attr, values in COVERED:
        for value in values:
            if (attr, value) not in have:
                raise SceneError(f"{where}: no scene with {attr} = {json.dumps(value)}")


def _known(where, at, table, allowed):
    for key in table:
        if key not in allowed:
            raise SceneError(f"{where}: unknown key {at}{key}")


def _need(where, at, table, key):
    if key not in table:
        raise SceneError(f"{where}: missing key {at}{key}")
    return table[key]


def _choice(where, at, table, key, options):
    value = _need(where, at, table, key)
    if value not in options:
        raise SceneError(f"{where}: {at}{key} must be one of {', '.join(options)}: {value!r}")
    return value


def _match(where, at, table, key, pattern, shape):
    value = _need(where, at, table, key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise SceneError(f"{where}: {at}{key} must be {shape}: {value!r}")
    return value


def _text(where, at, table, key):
    value = _need(where, at, table, key)
    if not isinstance(value, str) or not value.strip():
        raise SceneError(f"{where}: {at}{key} must be a non-empty string")
    return value


def _whole(where, at, table, key, low, high=None):
    """An int that is not a bool, from low up to high (or with no upper bound when high is None)"""
    value = _need(where, at, table, key)
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        span = f"{low} to {high}" if high is not None else f"at least {low}"
        raise SceneError(f"{where}: {at}{key} must be a whole number, {span}: {value!r}")
    return value


def _destination(where, at, table, key):
    """A destination: an index from 0, or a name"""
    value = _need(where, at, table, key)
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise SceneError(f"{where}: {at}{key} must be an index from 0 or a name: {value!r}")
