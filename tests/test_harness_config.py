"""scripts/harness/config.py, hx's private configuration, and scripts/harness/hx.py, the frozen shared entry point.

A good file loads from $HX_CONFIG, from an explicit path and from DEFAULT_PATH (under the home folder). A missing file,
invalid TOML, a missing key, a relative path, an empty value, no devices, a non-string env value and an unknown key each
raise ConfigError naming the file and the key. hx.py is byte-for-byte the shared copy.

python -I -B tests/test_harness_config.py
"""
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "harness"
HX_SHA256 = "d1cd2df6e2199d9a767b3e81aaec13c1b097b57e3d8151395c3f6aafa9cf1681"
sys.path.insert(0, str(HARNESS))
import config

# A good file: each top-level value as TOML text, and the device tables after them.
TOP = {
    "host": '"example-host"',
    "hold": '"/example/hold/hookshot.hold"',
    "root": '"/example/harness"',
    "dump": '"/example/dump"',
    "enter_closure": '"/example/enter-closure.sh"',
}
DEVICES = (
    "[devices.first]\n"
    'expect = "Example GPU One"\n'
    'env = { EXAMPLE_VAR = "one" }\n'
    "\n"
    "[devices.second]\n"
    'expect = "Example GPU Two"\n'
)


def toml(top=None, devices=DEVICES):
    """The file's text: TOP with each key in top replaced (None drops the key), then the device tables"""
    values = {**TOP, **(top or {})}
    lines = ["%s = %s" % (k, v) for k, v in values.items() if v is not None]
    return "\n".join(lines) + "\n" + devices


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, text, name="harness.toml"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return path

    def assert_refused(self, path, *keys):
        with self.assertRaises(config.ConfigError) as cm:
            config.load(path)
        message = str(cm.exception)
        self.assertIn(path, message)
        rest = message.replace(path, "")   # (the file's own name is not the key: check what the message says apart from it)
        for key in keys:
            self.assertIn(key, rest)

    def test_explicit_path_loads_every_field(self):
        path = self.write(toml())
        cfg = config.load(path)
        self.assertEqual(cfg.path, path)
        self.assertEqual(cfg.host, "example-host")
        self.assertEqual(cfg.hold, "/example/hold/hookshot.hold")
        self.assertEqual(cfg.root, "/example/harness")
        self.assertEqual(cfg.dump, "/example/dump")
        self.assertEqual(cfg.enter_closure, "/example/enter-closure.sh")
        self.assertEqual(sorted(cfg.devices), ["first", "second"])
        self.assertEqual(cfg.devices["first"],
                         config.Device(name="first", expect="Example GPU One", env={"EXAMPLE_VAR": "one"}))
        self.assertEqual(cfg.devices["second"], config.Device(name="second", expect="Example GPU Two", env={}))

    def test_hx_config_names_the_file(self):
        path = self.write(toml())
        with mock.patch.dict(os.environ, {config.ENV: path}):
            cfg = config.load()
        self.assertEqual(cfg.path, path)
        self.assertEqual(cfg.host, "example-host")

    def test_explicit_path_wins_over_hx_config(self):
        path = self.write(toml())
        with mock.patch.dict(os.environ, {config.ENV: os.path.join(self.tmp.name, "absent.toml")}):
            cfg = config.load(path)
        self.assertEqual(cfg.path, path)

    def test_default_path_is_read_when_nothing_else_names_a_file(self):
        target = os.path.join(self.tmp.name, "hookshot-harness", "config", "harness.toml")
        os.makedirs(os.path.dirname(target))
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(toml())
        with mock.patch.dict(os.environ, {"HOME": self.tmp.name, "USERPROFILE": self.tmp.name}):
            os.environ.pop(config.ENV, None)
            cfg = config.load()
        self.assertEqual(os.path.normpath(cfg.path), os.path.normpath(target))
        self.assertEqual(cfg.host, "example-host")

    def test_missing_file_is_refused_naming_the_file(self):
        path = os.path.join(self.tmp.name, "absent.toml")
        self.assert_refused(path)

    def test_invalid_toml_is_refused_naming_the_file(self):
        path = self.write("host = \n")
        self.assert_refused(path)

    def test_missing_key_is_refused_naming_the_key(self):
        for key in TOP:
            with self.subTest(key=key):
                path = self.write(toml({key: None}))
                self.assert_refused(path, key)

    def test_empty_or_non_string_value_is_refused_naming_the_key(self):
        for key in TOP:
            for value in ('""', "1"):
                with self.subTest(key=key, value=value):
                    path = self.write(toml({key: value}))
                    self.assert_refused(path, key)

    def test_relative_path_is_refused_naming_the_key(self):
        for key in ("hold", "root", "dump", "enter_closure"):
            with self.subTest(key=key):
                path = self.write(toml({key: '"relative/path"'}))
                self.assert_refused(path, key)

    def test_no_devices_is_refused_naming_devices(self):
        for devices in ("", "[devices]\n"):
            with self.subTest(devices=devices):
                path = self.write(toml(devices=devices))
                self.assert_refused(path, "devices")

    def test_missing_or_empty_expect_is_refused_naming_it(self):
        for body in ("", 'expect = ""\n'):
            with self.subTest(body=body):
                path = self.write(toml(devices="[devices.first]\n" + body))
                self.assert_refused(path, "expect")

    def test_non_string_env_value_is_refused_naming_the_variable(self):
        path = self.write(toml(devices='[devices.first]\nexpect = "Example GPU One"\nenv = { EXAMPLE_VAR = 1 }\n'))
        self.assert_refused(path, "env", "EXAMPLE_VAR")

    def test_env_that_is_not_a_table_is_refused_naming_env(self):
        path = self.write(toml(devices='[devices.first]\nexpect = "Example GPU One"\nenv = "EXAMPLE_VAR=one"\n'))
        self.assert_refused(path, "env")

    def test_unknown_top_level_key_is_refused_naming_it(self):
        path = self.write(toml({"hosst": '"example-host"'}))
        self.assert_refused(path, "hosst")

    def test_unknown_device_key_is_refused_naming_it(self):
        path = self.write(toml(devices='[devices.first]\nexpect = "Example GPU One"\nexpct = "typo"\n'))
        self.assert_refused(path, "expct")


class HxTest(unittest.TestCase):
    def test_hx_py_is_the_frozen_shared_copy(self):
        digest = hashlib.sha256((HARNESS / "hx.py").read_bytes()).hexdigest()
        self.assertEqual(digest, HX_SHA256)


if __name__ == "__main__":
    unittest.main()
