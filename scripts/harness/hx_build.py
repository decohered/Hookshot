"""hx build <rev>: builds a revision on the build host. This checkout's devenv closure and the revision's source go to the host,
make_game.py runs inside the closure as a build job, and then the closure, the full commit id, this checkout's dirty diff
and the built binaries' sha256 hashes are printed. The same record is kept beside the build on the host (build.txt, and
dirty.diff when the checkout is dirty), and builds/latest points at the build. make_game's console output goes to the
build's console.log on the host, and a failed build prints that log's last lines.

hx build --base <rev>: builds a development base instead (scripts/dev_packs.py base, docs/development.md). The revision's
history goes to the host as a git bundle, and one job checks the revision out from it, extracts the game dump and builds the
base into <root>/bases/<sha>. The record names the base's module hashes and build_s, and is kept there too; builds/latest is
not changed.

hx build --pack <rev> --on <base rev>: builds a change over a kept base (scripts/dev_packs.py pack, docs/development.md). Before
anything reaches the host, the base must be an ancestor of the revision, and dev_packs.py's route must call the change
renderer-only; otherwise it is refused with exit 2, naming the files that make it the game's. The history after the base goes to
the host as a bundle, and one job builds the pack in the base's source. The record names the renderer's hash and build_s, and is
kept in <root>/bases/<base sha>/hx-packs/<sha>.
"""
import hashlib
import io
import json
import os
import posixpath
import runpy
import shlex
import subprocess
import sys
import tarfile
import tempfile

import config
import jobs
import remote

BINARIES = ("hookshot", "hookshot_vk.so")
RENDERER = BINARIES[1]
STORE = "/nix/store"
BUNDLE = "src.bundle"
BASE_REF = "refs/heads/hx-base"   # (the branch the bundle names, in the temporary clone that makes it)
USAGE = "usage: hx build [--base] <rev>, or hx build --pack <rev> --on <base rev>"
q = shlex.quote


class BuildError(Exception):
    pass


def resolve(rev):
    """rev's full commit id in this checkout, or None when rev names no commit here"""
    if rev.startswith("-"):
        return None
    proc = subprocess.run(["git", "-C", str(jobs.CHECKOUT), "rev-parse", "--verify", "--quiet", rev + "^{commit}"],
                          capture_output=True, encoding="utf-8")
    return proc.stdout.strip() if proc.returncode == 0 else None


def devenv_closure():
    """This checkout's devenv closure: devenv shell makes .devenv/gc/shell, a link to the store path it is. devenv's output
    is printed only when devenv shell -- true fails."""
    proc = subprocess.run(["devenv", "shell", "--", "true"], cwd=str(jobs.CHECKOUT), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT)
    if proc.returncode:
        sys.stderr.flush()
        sys.stderr.buffer.write(proc.stdout)
        sys.stderr.buffer.flush()
        raise BuildError("devenv shell -- true failed with status %d" % proc.returncode)
    link = os.path.join(str(jobs.CHECKOUT), ".devenv", "gc", "shell")
    closure = os.path.realpath(link)
    if os.path.dirname(closure) != STORE:
        raise BuildError("%s does not link to a path directly under %s/: %r" % (link, STORE, closure))
    return closure


def pin(cfg, closure):
    """Keeps the closure on the host with a gc root under the harness root"""
    gc = posixpath.join(cfg.root, "gc")
    remote.run(cfg.host, "mkdir -p %s && nix-store --add-root %s --realise %s\n" % (
        q(gc), q(posixpath.join(gc, posixpath.basename(closure))), q(closure)))


def ready_closure(cfg):
    """Before the host: this checkout's dirty diff and devenv closure, the diff checked unchanged, then the closure copied to
    the host and pinned there. Returns the diff and the closure's path."""
    diff = dirty_diff()
    closure = devenv_closure()
    if dirty_diff() != diff:
        raise BuildError("the checkout changed while the devenv closure was evaluated; nothing was copied, "
                         "so run hx build again")
    remote.copy_closure(cfg.host, closure)
    pin(cfg, closure)
    return diff, closure


def archive(sha):
    """The revision's tree as the bytes of a tar, from git archive"""
    proc = subprocess.run(["git", "-C", str(jobs.CHECKOUT), "archive", "--format=tar", sha], capture_output=True)
    if proc.returncode:
        raise BuildError("git archive %s failed: %s" % (sha, proc.stderr.decode("utf-8", "replace").strip()))
    return proc.stdout


def unpack(cfg, sha, bd):
    """The revision's tree, from git archive, unpacked into the build folder's src on the host. The folder's build.txt and
    dirty.diff are left as they are: the build job removes them when it starts (start_build)."""
    src = posixpath.join(bd, "src")
    remote.run(cfg.host, "mkdir -p %s && tar -xf - -C %s\n" % (q(src), q(src)), input=archive(sha))


def history_bundle(sha, exclude=None):
    """The commit's history as the bytes of a git bundle: with exclude (a commit the receiver has), only what is newer than it.
    git bundle takes refs, so the commit is named by a branch of a temporary clone that shares this checkout's objects: this
    checkout gains no ref."""
    with tempfile.TemporaryDirectory() as tmp:
        clone = os.path.join(tmp, "clone")
        bundle = os.path.join(tmp, BUNDLE)
        git_run(["clone", "-q", "--shared", "--no-checkout", str(jobs.CHECKOUT), clone])
        git_run(["-C", clone, "update-ref", BASE_REF, sha])
        git_run(["-C", clone, "bundle", "create", bundle, BASE_REF] + (["^" + exclude] if exclude else []))
        with open(bundle, "rb") as f:
            return f.read()


def git_run(args):
    """Runs git with args, returns its standard output as text, and raises BuildError with its message when it fails"""
    proc = subprocess.run(["git", *args], capture_output=True)
    if proc.returncode:
        raise BuildError("git %s failed: %s" % (" ".join(args), proc.stderr.decode("utf-8", "replace").strip()))
    return proc.stdout.decode("utf-8", "replace")


def start_build(cfg, job_id, closure, bd, command):
    """Starts command as a job, through the closure's entry script. The job first removes the build folder's earlier
    build.txt, dirty.diff and console.log, so the folder holds the record of the build that last succeeded there, or none.
    The command's stdout and stderr go to console.log. A start that is refused runs nothing and removes nothing."""
    console = posixpath.join(bd, "console.log")
    records = [posixpath.join(bd, name) for name in ("build.txt", "dirty.diff")]
    script = 'c=$1 && rm -f -- "$c" "$2" "$3" && shift 3 && exec "$@" >"$c" 2>&1'
    argv = ["sh", "-c", script, "hx-build", console] + records + [cfg.enter_closure, closure] + command
    jobs.start(cfg, job_id, argv, env={"XDG_CACHE_HOME": posixpath.join(cfg.root, "cache"),
                                       "HOOKSHOT_NO_UPDATE_CHECK": "1"})


def follow_build(cfg, job_id, bd):
    """Prints the build job's output to its end and returns its status (0: it built). A lost ssh returns 255 and the job goes
    on; a failed build prints its status, its console log's path and the log's last 20 lines."""
    status = jobs.follow(cfg, job_id)
    if status == 255:
        print("ssh lost: the build goes on as %s (build folder %s)" % (jobs.unit(job_id), bd))
        return 255
    if status:
        console = posixpath.join(bd, "console.log")
        print("build failed: exit status %d; log: %s" % (status, console))
        tail = remote.run(cfg.host, "tail -n 20 %s\n" % q(console), check=False)
        sys.stdout.write(tail.stdout)
        if tail.returncode:
            print("hx build: could not read %s: %s" % (console, tail.stderr.strip()), file=sys.stderr)
    return status


def binary_hashes(cfg, bd):
    """The sha256 of each built binary, in BINARIES order, as sha256sum prints them on the host"""
    game = posixpath.join(bd, "game")
    out = remote.run(cfg.host, "cd %s && sha256sum %s\n" % (q(game), " ".join(map(q, BINARIES)))).stdout
    rows = [line.split(None, 1) for line in out.splitlines()]
    if [name for _, name in rows] != list(BINARIES):
        raise BuildError("sha256sum printed %r for %s" % (out, ", ".join(BINARIES)))
    return [digest for digest, _ in rows]


def base_json(cfg, bd):
    """The sha256 of each module in BINARIES order, and build_s, from the base.json dev_packs.py base wrote on the host"""
    path = posixpath.join(bd, "base.json")
    text = remote.run(cfg.host, "cat %s\n" % q(path)).stdout
    try:
        info = json.loads(text)
        return [info["modules"][name] for name in BINARIES], info["build_s"]
    except (ValueError, KeyError, TypeError) as e:
        raise BuildError("%s is not a base record: %s" % (path, e)) from None


def dirty_diff():
    """This checkout's uncommitted changes against HEAD, as git diff writes them with --binary"""
    proc = subprocess.run(["git", "-C", str(jobs.CHECKOUT), "diff", "HEAD", "--binary"], capture_output=True)
    if proc.returncode:
        raise BuildError("git diff HEAD failed: %s" % proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout


def dirty_value(diff):
    return hashlib.sha256(diff).hexdigest() if diff else "none"


def record_text(fields):
    return "".join("%s: %s\n" % field for field in fields)


def put(cfg, path, data):
    remote.run(cfg.host, "cat > %s\n" % q(path), input=data)


def keep_record(cfg, bd, text, diff):
    """The record beside the build on the host: build.txt, and dirty.diff when the checkout was dirty"""
    put(cfg, posixpath.join(bd, "build.txt"), text.encode("utf-8"))
    if diff:
        put(cfg, posixpath.join(bd, "dirty.diff"), diff)


def show_record(text, diff):
    sys.stdout.write(text)
    if diff:
        sys.stdout.flush()
        sys.stdout.buffer.write(diff)
        sys.stdout.buffer.flush()


def build(cfg, sha):
    diff, closure = ready_closure(cfg)
    bd = jobs.build_dir(cfg, sha)
    unpack(cfg, sha, bd)
    job_id = jobs.new_id("build")
    make_game = ["python", "-I", posixpath.join(bd, "src", "nx2", "make_game.py"),
                 "--nup", cfg.dump, "--out", posixpath.join(bd, "game"), "--log", posixpath.join(bd, "build.log")]
    start_build(cfg, job_id, closure, bd, make_game)
    status = follow_build(cfg, job_id, bd)
    if status:
        return status
    digests = binary_hashes(cfg, bd)
    fields = [("closure", closure), ("sha", sha), ("dirty", dirty_value(diff)), ("hookshot", digests[0]),
              ("hookshot_vk.so", digests[1]), ("build", bd), ("job", job_id)]
    text = record_text(fields)
    keep_record(cfg, bd, text, diff)
    remote.run(cfg.host, "ln -sfn %s %s\n" % (q(bd), q(posixpath.join(cfg.root, "builds", "latest"))))
    show_record(text, diff)
    return 0


def base(cfg, sha):
    diff, closure = ready_closure(cfg)
    bd = posixpath.join(cfg.root, "bases", sha)
    remote.run(cfg.host, "mkdir -p %s\n" % q(bd))
    put(cfg, posixpath.join(bd, BUNDLE), history_bundle(sha))
    job_id = jobs.new_id("build")
    in_job = ["python", "-I", posixpath.join(jobs.job_dir(cfg, job_id), "tools", "scripts", "harness", "hx.py"),
              "build", "--in-job", bd, sha, cfg.dump]
    start_build(cfg, job_id, closure, bd, in_job)
    status = follow_build(cfg, job_id, bd)
    if status:
        return status
    digests, build_s = base_json(cfg, bd)
    fields = [("closure", closure), ("sha", sha), ("dirty", dirty_value(diff)), ("hookshot", digests[0]),
              ("hookshot_vk.so", digests[1]), ("build_s", str(build_s)), ("base", bd), ("job", job_id)]
    text = record_text(fields)
    keep_record(cfg, bd, text, diff)
    show_record(text, diff)
    return 0


def dev_packs_route():
    """The route of this checkout's scripts/dev_packs.py, the rule for which files a module is built from. The file's top level
    only defines things: its main runs under __main__, so running the file as a script gives the function and nothing else."""
    return runpy.run_path(str(jobs.CHECKOUT / "scripts" / "dev_packs.py"))["route"]


def extract(data, folder):
    """A git archive's tar unpacked into folder. The data filter refuses an absolute path, a path out of folder, or a link that
    leads out of it, so what is read for the route is read only inside folder."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(folder, filter="data")
    except tarfile.TarError as e:
        raise BuildError("the tree cannot be extracted safely: %s" % e) from None


def pack_refusal(sha, base_sha):
    """Why hx build --pack refuses sha on base_sha, or None. The base must be an ancestor of sha, and dev_packs.py's route, of
    the files that differ between them with sha's tree as its root, must say renderer. Otherwise the reason names the files
    the route calls the game's, or says that nothing a module is built from changed."""
    if subprocess.run(["git", "-C", str(jobs.CHECKOUT), "merge-base", "--is-ancestor", base_sha, sha],
                      capture_output=True).returncode:
        return "%s is not built on the base %s: rebase it on the base, or build a new base" % (sha[:8], base_sha[:8])
    files = [f for f in git_run(["-C", str(jobs.CHECKOUT), "diff", "--name-only", base_sha, sha]).split("\n") if f]
    route = dev_packs_route()
    with tempfile.TemporaryDirectory() as tmp:
        extract(archive(sha), tmp)
        if route(files, tmp) == "renderer":
            return None
        game = [f for f in files if route([f], tmp) == "game"]
    if game:
        return "not renderer-only: these changed files are built with the game: %s" % ", ".join(game)
    return "not renderer-only: nothing a module is built from changed between %s and %s" % (base_sha[:8], sha[:8])


def kept_base(cfg, base_sha, bd):
    """The base's module hashes, from the base.json on the host, read before any job starts: a pack is built on a kept base"""
    try:
        return base_json(cfg, bd)[0]
    except remote.RemoteError as e:
        raise BuildError("no kept base for %s in %s (%s): build it first with hx build --base"
                         % (base_sha[:8], bd, e)) from None


def pack_json(cfg, bd, base_sha, sha):
    """The modules' hashes dev_packs.py pack wrote, and its build_s, from the pack.json on the host. It must name this base,
    this commit and the renderer route: a pack that names another is not recorded (F3)"""
    path = posixpath.join(bd, "packs", sha, "pack.json")
    text = remote.run(cfg.host, "cat %s\n" % q(path)).stdout
    try:
        info = json.loads(text)
        named = (info["base"], info["head"], info["route"])
        files, build_s = info["files"], info["build_s"]
    except (ValueError, KeyError, TypeError) as e:
        raise BuildError("%s is not a pack record: %s" % (path, e)) from None
    if named != (base_sha, sha, "renderer"):
        raise BuildError("%s names base %s, head %s and route %s, not base %s, head %s and route renderer: not recorded"
                         % (path, named[0], named[1], named[2], base_sha, sha))
    if not isinstance(files, dict):
        raise BuildError("%s is not a pack record: files is not a map" % path)
    return files, build_s


def pack(cfg, sha, base_sha):
    diff, closure = ready_closure(cfg)
    bd = posixpath.join(cfg.root, "bases", base_sha)
    base_digests = kept_base(cfg, base_sha, bd)
    pd = posixpath.join(bd, "hx-packs", sha)
    remote.run(cfg.host, "mkdir -p %s\n" % q(pd))
    put(cfg, posixpath.join(pd, BUNDLE), history_bundle(sha, base_sha))
    job_id = jobs.new_id("build")
    in_pack = ["python", "-I", posixpath.join(jobs.job_dir(cfg, job_id), "tools", "scripts", "harness", "hx.py"),
               "build", "--in-pack", bd, sha]
    start_build(cfg, job_id, closure, pd, in_pack)
    status = follow_build(cfg, job_id, pd)
    if status:
        return status
    files, build_s = pack_json(cfg, bd, base_sha, sha)
    fields = [("closure", closure), ("sha", sha), ("base", base_sha), ("dirty", dirty_value(diff)),
              ("hookshot_vk.so", files.get(RENDERER, base_digests[1])), ("build_s", str(build_s)),
              ("pack", posixpath.join(bd, "packs", sha)), ("job", job_id)]
    text = record_text(fields)
    keep_record(cfg, pd, text, diff)
    show_record(text, diff)
    return 0


def pack_rev(rev, on):
    """hx build --pack <rev> --on <base rev>: refused (exit 2) before anything reaches the host when a rev is unknown, the base
    is no ancestor of the change, or the change is not renderer-only; otherwise the change is built over the base on the host"""
    sha, base_sha = resolve(rev), resolve(on)
    for name, value in ((rev, sha), (on, base_sha)):
        if value is None:
            print("hx build: %r is not a commit in this checkout" % name, file=sys.stderr)
            return 2
    refusal = pack_refusal(sha, base_sha)
    if refusal:
        print("hx build --pack: refused: %s" % refusal, file=sys.stderr)
        return 2
    return pack(config.load(), sha, base_sha)


def run_steps(label, steps):
    """Runs each step (its name and command) in turn, its output on the console log. The first that fails ends the job with its
    status; label names the command in the messages."""
    for name, cmd in steps:
        try:
            status = subprocess.run(cmd).returncode
        except OSError as e:
            print("%s: %s could not start: %s" % (label, name, e), flush=True)
            return 1
        if status:
            print("%s: %s failed: exit status %d" % (label, name, status), flush=True)
            return status
    return 0


def in_job(argv):
    """The in-job half of hx build --base, run inside the base's job on the host (from the job's tools, as hx.py build).
    The steps run in turn, each printing its failure to the console log: the revision is cloned from the bundle and checked
    out at its sha, the game dump is extracted, and dev_packs.py base builds the base from that source. The first step that
    fails ends the job with its status."""
    if len(argv) != 3:
        print("usage: hx build --in-job <base folder> <sha> <dump>", file=sys.stderr)
        return 2
    bd, sha, dump = argv
    src = posixpath.join(bd, "src")
    extract = posixpath.join(bd, "dump")
    steps = [
        ("git clone", ["git", "clone", "-q", "--no-checkout", posixpath.join(bd, BUNDLE), src]),
        ("git checkout", ["git", "-C", src, "checkout", "-q", "--detach", sha]),
        ("nx2/nup.py", [sys.executable, "-I", posixpath.join(src, "nx2", "nup.py"), dump, "--extract", extract]),
        ("scripts/dev_packs.py base", [sys.executable, "-I", posixpath.join(src, "scripts", "dev_packs.py"), "base", bd,
                                       "--romfs", posixpath.join(extract, "romfs"),
                                       "--exefs", posixpath.join(extract, "exefs"), "--tree", src]),
    ]
    return run_steps("hx build --base", steps)


def in_pack(argv):
    """The in-job half of hx build --pack, run inside the pack's job on the host (from the job's tools, as hx.py build). The
    pack's history is fetched from its bundle into the base's source; the commit gets a detached worktree in <base>/trees unless
    that folder is there already (pack.json then checks the head it was built at); the base's own dev_packs.py pack builds it.
    The first step that fails ends the job with its status."""
    if len(argv) != 2:
        print("usage: hx build --in-pack <base folder> <sha>", file=sys.stderr)
        return 2
    bd, sha = argv
    src = posixpath.join(bd, "src")
    tree = posixpath.join(bd, "trees", sha)
    steps = [("git fetch", ["git", "-C", src, "fetch", "-q", posixpath.join(bd, "hx-packs", sha, BUNDLE), BASE_REF])]
    if not os.path.exists(tree):
        steps.append(("git worktree", ["git", "-C", src, "worktree", "add", "-q", "--detach", tree, sha]))
    steps.append(("scripts/dev_packs.py pack", [sys.executable, "-I", posixpath.join(src, "scripts", "dev_packs.py"), "pack",
                                                bd, sha, "--tree", tree]))
    return run_steps("hx build --pack", steps)


def main(argv):
    if argv[:1] == ["--in-job"]:
        return in_job(argv[1:])
    if argv[:1] == ["--in-pack"]:
        return in_pack(argv[1:])
    try:
        if argv[:1] == ["--pack"]:
            if len(argv) != 4 or argv[2] != "--on":
                print(USAGE, file=sys.stderr)
                return 2
            return pack_rev(argv[1], argv[3])
        as_base = argv[:1] == ["--base"]
        revs = argv[1:] if as_base else argv
        if len(revs) != 1:
            print(USAGE, file=sys.stderr)
            return 2
        rev = revs[0]
        sha = resolve(rev)
        if sha is None:
            print("hx build: %r is not a commit in this checkout" % rev, file=sys.stderr)
            return 2
        cfg = config.load()
        return base(cfg, sha) if as_base else build(cfg, sha)
    except (BuildError, config.ConfigError, jobs.JobError, remote.RemoteError, OSError) as e:
        print("hx build: %s" % e, file=sys.stderr)
        return 1
