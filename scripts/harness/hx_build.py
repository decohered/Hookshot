"""hx build <rev>: builds a revision on the build host. This checkout's devenv closure and the revision's source go to the host,
make_game.py runs inside the closure as a build job, and then the closure, the full commit id, this checkout's dirty diff
and the built binaries' sha256 hashes are printed. The same record is kept beside the build on the host (build.txt, and
dirty.diff when the checkout is dirty), and builds/latest points at the build. make_game's console output goes to the
build's console.log on the host, and a failed build prints that log's last lines.
"""
import hashlib
import os
import posixpath
import shlex
import subprocess
import sys

import config
import jobs
import remote

BINARIES = ("hookshot", "hookshot_vk.so")
STORE = "/nix/store"
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


def unpack(cfg, sha, bd):
    """The revision's tree, from git archive, unpacked into the build folder's src on the host. The folder's build.txt and
    dirty.diff are left as they are: the build job removes them when it starts (start_build)."""
    proc = subprocess.run(["git", "-C", str(jobs.CHECKOUT), "archive", "--format=tar", sha], capture_output=True)
    if proc.returncode:
        raise BuildError("git archive %s failed: %s" % (sha, proc.stderr.decode("utf-8", "replace").strip()))
    src = posixpath.join(bd, "src")
    remote.run(cfg.host, "mkdir -p %s && tar -xf - -C %s\n" % (q(src), q(src)), input=proc.stdout)


def start_build(cfg, job_id, closure, bd):
    """Starts make_game.py as a job, through the closure's entry script. The job first removes the build folder's earlier
    build.txt, dirty.diff and console.log, so the folder holds the record of the build that last succeeded there, or none.
    make_game's stdout and stderr go to console.log. A start that is refused runs nothing and removes nothing."""
    console = posixpath.join(bd, "console.log")
    records = [posixpath.join(bd, name) for name in ("build.txt", "dirty.diff")]
    script = 'c=$1 && rm -f -- "$c" "$2" "$3" && shift 3 && exec "$@" >"$c" 2>&1'
    argv = ["sh", "-c", script, "hx-build", console] + records + [
        cfg.enter_closure, closure, "python", "-I", posixpath.join(bd, "src", "nx2", "make_game.py"),
        "--nup", cfg.dump, "--out", posixpath.join(bd, "game"), "--log", posixpath.join(bd, "build.log")]
    jobs.start(cfg, job_id, argv, env={"XDG_CACHE_HOME": posixpath.join(cfg.root, "cache"),
                                       "HOOKSHOT_NO_UPDATE_CHECK": "1"})


def binary_hashes(cfg, bd):
    """The sha256 of each built binary, in BINARIES order, as sha256sum prints them on the host"""
    game = posixpath.join(bd, "game")
    out = remote.run(cfg.host, "cd %s && sha256sum %s\n" % (q(game), " ".join(map(q, BINARIES)))).stdout
    rows = [line.split(None, 1) for line in out.splitlines()]
    if [name for _, name in rows] != list(BINARIES):
        raise BuildError("sha256sum printed %r for %s" % (out, ", ".join(BINARIES)))
    return [digest for digest, _ in rows]


def dirty_diff():
    """This checkout's uncommitted changes against HEAD, as git diff writes them with --binary"""
    proc = subprocess.run(["git", "-C", str(jobs.CHECKOUT), "diff", "HEAD", "--binary"], capture_output=True)
    if proc.returncode:
        raise BuildError("git diff HEAD failed: %s" % proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout


def put(cfg, path, data):
    remote.run(cfg.host, "cat > %s\n" % q(path), input=data)


def build(cfg, sha):
    diff = dirty_diff()
    closure = devenv_closure()
    if dirty_diff() != diff:
        raise BuildError("the checkout changed while the devenv closure was evaluated; nothing was copied, "
                         "so run hx build again")
    remote.copy_closure(cfg.host, closure)
    pin(cfg, closure)
    bd = jobs.build_dir(cfg, sha)
    unpack(cfg, sha, bd)
    job_id = jobs.new_id("build")
    start_build(cfg, job_id, closure, bd)
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
    digests = binary_hashes(cfg, bd)
    dirty = hashlib.sha256(diff).hexdigest() if diff else "none"
    fields = [("closure", closure), ("sha", sha), ("dirty", dirty), ("hookshot", digests[0]),
              ("hookshot_vk.so", digests[1]), ("build", bd), ("job", job_id)]
    text = "".join("%s: %s\n" % field for field in fields)
    put(cfg, posixpath.join(bd, "build.txt"), text.encode("utf-8"))
    if diff:
        put(cfg, posixpath.join(bd, "dirty.diff"), diff)
    remote.run(cfg.host, "ln -sfn %s %s\n" % (q(bd), q(posixpath.join(cfg.root, "builds", "latest"))))
    sys.stdout.write(text)
    if diff:
        sys.stdout.flush()
        sys.stdout.buffer.write(diff)
        sys.stdout.buffer.flush()
    return 0


def main(argv):
    if len(argv) != 1:
        print("usage: hx build <rev>", file=sys.stderr)
        return 2
    rev = argv[0]
    try:
        sha = resolve(rev)
        if sha is None:
            print("hx build: %r is not a commit in this checkout" % rev, file=sys.stderr)
            return 2
        return build(config.load(), sha)
    except (BuildError, config.ConfigError, jobs.JobError, remote.RemoteError, OSError) as e:
        print("hx build: %s" % e, file=sys.stderr)
        return 1
