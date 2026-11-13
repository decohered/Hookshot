"""hx: the Hookshot harness command line.

Usage: python -I scripts/harness/hx.py <command> [arguments]

Each command lives beside this file as hx_<command>.py and defines main(argv) -> int.
"""
import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent


def commands():
    return sorted(p.stem[len("hx_"):] for p in HERE.glob("hx_*.py"))


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        print("Commands: " + (", ".join(commands()) or "none"))
        return 0 if argv else 2
    path = HERE / f"hx_{argv[0]}.py"
    if not path.is_file():
        print(f"hx: no command {argv[0]!r}; commands: {', '.join(commands()) or 'none'}", file=sys.stderr)
        return 2
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
