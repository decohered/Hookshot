"""hx's private configuration: the host, the hold file, the harness root, the game dump, the closure-entry script and the
devices, read from one TOML file. load() returns it validated and frozen. A missing or unreadable file, invalid TOML or
a bad entry raises ConfigError naming the file and the key.

    host = "example-host"
    hold = "/abs/hold-file"
    root = "/abs/harness-root"
    dump = "/abs/game-dump"
    enter_closure = "/abs/enter-closure-script"

    [devices.<name>]
    expect = "the exact Vulkan device name the renderer reports"
    env = { NAME = "value" }   # optional: the launch's environment for this GPU
"""
import os
import tomllib
from dataclasses import dataclass, field

DEFAULT_PATH = "~/hookshot-harness/config/harness.toml"
ENV = "HX_CONFIG"
TOP_KEYS = ("host", "hold", "root", "dump", "enter_closure", "devices")
ABSOLUTE_KEYS = ("hold", "root", "dump", "enter_closure")
DEVICE_KEYS = ("expect", "env")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Device:
    name: str
    expect: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    path: str
    host: str
    hold: str
    root: str
    dump: str
    enter_closure: str
    devices: dict[str, Device]


def load(path=None):
    """The configuration in path, else in $HX_CONFIG, else in DEFAULT_PATH"""
    if path is None:
        path = os.environ.get(ENV) or os.path.expanduser(DEFAULT_PATH)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise ConfigError(f"{path}: cannot read it: {e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: not valid TOML: {e}") from None
    _known(path, "", data, TOP_KEYS)
    host = _text(path, "", data, "host")
    paths = {k: _absolute(path, "", data, k) for k in ABSOLUTE_KEYS}
    return Config(path=path, host=host, devices=_devices(path, data), **paths)


def _devices(path, data):
    tables = _need(path, "", data, "devices")
    if not isinstance(tables, dict) or not tables:
        raise ConfigError(f"{path}: devices must hold at least one [devices.<name>] table")
    devices = {}
    for name, table in tables.items():
        if not isinstance(table, dict):
            raise ConfigError(f"{path}: devices.{name} must be a table")
        devices[name] = _device(path, name, table)
    return devices


def _device(path, name, table):
    where = f"devices.{name}."
    _known(path, where, table, DEVICE_KEYS)
    return Device(name=name, expect=_text(path, where, table, "expect"), env=_env(path, where, table))


def _env(path, where, table):
    if "env" not in table:
        return {}
    env = table["env"]
    if not isinstance(env, dict):
        raise ConfigError(f"{path}: {where}env must be a table of strings")
    for name, value in env.items():
        if not isinstance(value, str):
            raise ConfigError(f"{path}: {where}env.{name} must be a string")
    return env


def _known(path, where, table, allowed):
    for key in table:
        if key not in allowed:
            raise ConfigError(f"{path}: unknown key {where}{key}")


def _need(path, where, table, key):
    if key not in table:
        raise ConfigError(f"{path}: missing key {where}{key}")
    return table[key]


def _text(path, where, table, key):
    value = _need(path, where, table, key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: {where}{key} must be a non-empty string")
    return value


def _absolute(path, where, table, key):
    value = _text(path, where, table, key)
    if not value.startswith("/"):
        raise ConfigError(f"{path}: {where}{key} must be an absolute path starting with /: {value!r}")
    return value
