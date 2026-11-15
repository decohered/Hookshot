# The harness: hx commands for agents

How to run the harness's commands, what each one records and prints, and how to report what they found. The harness
measures builds of this checkout on a build host and keeps every result as a record run. Agents read this page before
they run a command.

## Running it

Run every command from the root of the checkout that the builds come from:

```
python -I scripts/harness/hx.py <command> [arguments]
```

`python -I scripts/harness/hx.py -h` lists the commands. `-h` after a command prints its usage, except for `build` and
`stop`, which do not take it. An unknown command exits 2 and lists the commands.

`report`, `verify` and `mangohud` are the exception: they read and write only the machine they run on, and the record
and its journal lines are on the build host (`results/` under the harness root, and the host user's journal). Run them
on the build host, from a copy of `scripts/harness`, or give them `--root` (and `verify` its `--journal`) pointing at a
copy of the host's record and journal. Their default root is `~/hookshot-harness/results` in the home folder of the
machine they run on.

Some commands have an in-job half, which the client starts on the build host inside a job (`--in-job`,
`--correctness-in-job` for `floor`, and `--run-in-job` for one timed run of `standing`). Agents never call them.

Each section below gives the usage line as the command prints it, without `-h`. Placeholders: `D` a device of the
configuration, `S` a scene of the suite, `REV` a commit of this checkout, `ID` a six-digit record run id, `<job id>` a
job's id.

## Quote record ids, never your own summary

A result lives in the record. When you report what a harness command found, quote the record id it printed and the
verdict line it printed, both as printed.

- `hx run`, `hx opening`, `hx crawl` and `hx mangohud` print `run: ID`.
- `hx floor` prints `floor: ID`, `hx bench` prints `bench: ID`, `hx envbench` prints `envbench: ID`,
  `hx standing` prints `standing: ID`, and `hx calibrate` prints `calibration <series>: ID` for each of its two
  series.
- `hx standing` also prints `run: ID` for each of its timed runs.
- The runs a floor, bench, calibration, envbench or standing makes are record runs too. Their ids are in the `series`
  entries of that record.

`hx standing` prints no verdict line: quote its `standing: ID` line and its `interval of B against A` line, both as
printed.

Read a record back with `hx report <id>`, and check the whole record with `hx verify`. `hx report` lists a run's files
by name and hash; the verdict itself is in the file it names (`bench.json`, `envbench.json`, `floor.json`,
`calibration.json`, `scene.json`, `opening.json` or `standing.json`; a standing run's own check is in
`standing_run.json`), which is under the run's folder in the record root.

Do not write your own summary of a result in place of the printed line and id: no percentage, "faster", "passed" or
"no regression" of your own making, and never restate a result from a job's log or from memory. A job id (`bench-...`,
`check-...`, `build-...`) names a job, not a result, so it is not a record id.

## Configuration

Commands that run on the build host read their configuration from `~/hookshot-harness/config/harness.toml`. Set
`HX_CONFIG` to another file to read that one instead. `report`, `verify` and `mangohud` do not read it.

The file lives outside git. Never copy its values into docs, commits, pull requests or messages. It is TOML with these
keys:

| Key | What it is |
|---|---|
| `host` | the build host, as ssh names it |
| `hold` | an absolute path on the host: the hold file (see [The hold file](#the-hold-file)) |
| `root` | an absolute path on the host: the harness root, where builds, jobs, scenes and records are kept |
| `dump` | an absolute path on the host: the game dump that builds are made from |
| `enter_closure` | an absolute path on the host: the script that runs a command inside a build's closure |
| `devices` | one `[devices.D]` table per device |

Each device table has `expect`, the exact Vulkan device name the renderer reports, and an optional `env`, a table of
strings given to the game's launch on that device.

```toml
host = "HOST"
hold = "/PATH/hold"
root = "/PATH/harness"
dump = "/PATH/dump"
enter_closure = "/PATH/enter-closure"

[devices.D]
expect = "VULKAN DEVICE NAME"
env = { NAME = "VALUE" }
```

A missing key, an unknown key, a relative path or a file that cannot be read is refused, and the message names the file
and the key. The command exits 2 (1 for `build` and `stop`).

The harness root on the host holds:

- `builds/<full commit id>/`: one build's folder, and `builds/latest`, which points at the newest build.
- `jobs/<job id>/`: one job's folder, with its `output.log`.
- `scenes/`: `suite.toml` (the scenes and the `[method]` table that every floor, bench and calibration uses),
  `opening.toml`, `crawl.toml`, `saves/` and `costmaps/`.
- `results/`: the record, with `runs/` and `screenshots/`. No command writes a screenshot now, so `screenshots/` holds
  the screenshots of older runs.

## The hold file

The hold file is the path `hold` names on the host. Nothing in the harness creates it. Creating it stops launches, and
removing it lets them go on. While it exists:

- No job starts. The start is refused with status 75, and the command prints `held: <path> is present, so no job starts
  while it is` and exits 2 (`build` exits 1).
- A running job waits. A floor, bench, calibration, envbench or standing waits before every run. `run`, `smoke`,
  `opening` and `crawl` wait once, before their first game launch; a crawl's later restarts do not wait.
- The wait prints `hold present (<path>): waiting for it to go`, and when the file is gone, `hold gone: waited N s`.

A job checks the hold again just before its unit starts, so a hold that appears while the job's folder is copied still
stops it.

## Jobs

A command that runs on the build host starts a job: a transient systemd user unit, `hx-<job id>.service`, which keeps
running when the ssh session ends. The client prints `job: <job id>` and then follows the job's output; `hx build`
instead prints `job:` only in its summary after a successful build. A job id is `<kind>-<UTC time>-<six hex digits>`.
The kinds:

- `build`: `hx build`.
- `check`: `hx smoke`, `hx opening`.
- `bench`: `hx run`, `hx floor`, `hx bench`, `hx envbench`, `hx calibrate`, `hx crawl`, `hx standing`.

Rules the host applies:

- A bench job runs alone: it is refused while any harness job runs. A build or check job is refused while a bench job
  runs. Two check jobs may run together.
- When a job ends, its folder's size becomes the last size of its kind. A new job of that kind is refused while the
  harness root has less than twice that much free, with status 77 and `disk: refused: ...`. A size the filesystem cannot
  measure leaves the last size as it was.
- A refused start (75 held, 76 busy, 77 disk) starts no unit, and the command prints the reason. A start refused before
  the job's folder is made leaves no folder; a hold that appears while the folder is filled refuses it with 75 just
  before the unit starts and leaves the folder behind.
- When a job's command ends, its game copy is removed. Its output stays in its folder.
- If ssh is lost, the command exits 255 and the job goes on. `hx stop <job id>...` ends it, with all its processes.

## Commands

### `hx bench`

```
hx bench --device D --scene S --floor ID A B
```

Judges build B against build A, both commits of this checkout, on one device and scene, against a recorded floor. The
series is the floor's own (see `hx floor`): the A-B-B-A timed runs and one correctness run per arm, each through its
build's closure, waiting for the hold before each. The floor must be a recorded floor of the same device, scene and
method, or the bench is refused before any run.

Arguments:

- `A`, `B`: the commits of the two builds. Both builds must be on the host (`hx build` makes them).
- `--device D`: a device of the configuration.
- `--scene S`: a scene of the suite.
- `--floor ID`: the six-digit record run of a recorded floor of this device, scene and method.

Records one run, with `bench.json`: the outcome (`faster`, `slower`, `no change`, `void` or `failed`), the reason or the
void reasons, the interval, each arm's medians, the floor, both shas and the series. The bench is void when the gate
voids it, from the logs alone (no picture is compared): a correctness log has a flash line or a validation error that
A's log lacks, or any log has a hang line. It is failed when the series ends early or a run's frames cannot be read
back.

Prints `bench: ID`, then `verdict: faster`, `verdict: slower` or `verdict: no change` with the interval and the floor;
or `verdict: void (...)` with the reasons; or `verdict: failed (...)` with the reason.

**Exit status:**

- `0`: a verdict (faster, slower or no change) is recorded.
- `1`: the bench is void or failed, and recorded.
- `2`: the bench could not run: an unknown device, scene or build; a floor that is refused; a job that will not start; a
  record that is not written.
- `255`: ssh lost; the job goes on.

### `hx build`

```
hx build <rev>
```

Builds one commit of this checkout on the build host. The checkout's devenv closure is copied to the host and pinned
there, and the commit's tree is unpacked into the build's folder. A build job then runs `nx2/make_game.py` inside the
closure, and its output goes to `console.log`.

Arguments:

- `rev`: any name git resolves to a commit in this checkout: a full or short commit id, a branch or a tag. A name that
  starts with `-` is refused.

Records, in the build's folder `builds/<full commit id>/` on the host: `build.txt` (the sha, the closure, the dirty
hash, the sha256 of each binary, the build folder and the job id), `dirty.diff` when the checkout had uncommitted
changes, `console.log`, `build.log`, and `game/`. `builds/latest` is pointed at the build. A build first removes the
folder's earlier `build.txt` and `dirty.diff`, so the folder keeps the record of the build that last succeeded there,
or none.

Prints, on success: `closure:`, `sha:`, `dirty:` (the sha256 of the diff, or `none`), `hookshot:`,
`hookshot_vk.so:`, `build:` (the folder) and `job:`, then the diff itself when the checkout was dirty. On failure, it
prints `build failed: exit status N; log: <console.log>` and the last 20 lines of that log.

If the checkout changes while its closure is evaluated, nothing is copied, and the build refuses: run it again.

**Exit status:**

- `0`: the build is made and recorded.
- the build job's own status (not 0): the build failed; its log is named.
- `255`: ssh lost; the build goes on.
- `2`: `rev` is not a commit of this checkout.
- `1`: refused or failed before the build job ran: `devenv shell -- true` fails, a host command fails, the configuration
  is refused, or the job will not start (held, busy or disk). Also `1` when the build job succeeded but a later step
  failed: reading the binaries' hashes, or writing `build.txt`, `dirty.diff` or the `latest` link. The build folder may
  then hold a built game with no `build.txt`.

### `hx calibrate`

```
hx calibrate --device D --scene S [--build REV]
```

Calibrates the method against itself on one build, as one bench job with that build in both arms. Two series run in
order against the newest recorded floor of this device, scene and method. The floor is read from the record, never
measured here. Each series is judged the way `hx bench` judges it:

| Series | Planted in arm B's runs | Expects |
|---|---|---|
| `none` | nothing (A against A) | `no change` |
| `delay` | a submit delay: the smallest whole number of microseconds at or above 2 × floor × T, where floor is the floor as a fraction (`floor.json`'s `floor`) and T is the median of the floor's run medians converted to microseconds (ms × 1000), at least 1 | `slower` |

A calibration uses the newest recorded floor and never an older one. It is refused before any run when that floor was
measured on another build (its `a_sha` is not the calibrated build's sha), or when the device's environment sets
`RX_VK_ASYNC` to a non-zero number, because the planted delay sits only on the synchronous replay path. The refusal
prints `hx calibrate: could not run: <reason>` on stderr; for another build, the reason names both shas and the floor's
run id.

A series that does not match, or does not run through, is recorded, and the next series runs. Nothing is rerun.

Arguments:

- `--device D`, `--scene S`: as for `hx bench`.
- `--build REV`: the build of this checkout's commit `REV`. Without it, the newest build.

Records one run per series, with `calibration.json`: the series' name, the expected and actual outcome, whether they
match, the planted value, the floor's run id, and the series' runs.

Prints, per series: `calibration <series>: ID`, then `<series>: <outcome> (expected <outcome>): match` or `MISMATCH`, then
a line with the reason or the void reasons when there are any.

**Exit status:**

- `0`: both series match.
- `1`: a series does not match or does not run through, and is recorded. The other series still run.
- `2`: the calibration could not run (no recorded floor of this device, scene and method; the newest floor measured on
  another build; `RX_VK_ASYNC` set to a non-zero number in the device's environment; a floor with no run medians to size
  the delay from; a job that will not start), or a series' record is not written or the next series cannot start, which
  ends the calibration there.
- `255`: ssh lost; the job goes on.

### `hx crawl`

```
hx crawl --device D [--build REV]
```

Visits each destination the build's game offers, in each variant, starting from one save. At each destination the
game arrives, settles, and the camera turns once around (the right stick held for `pan_frames`) inside one window,
whose trace costs are kept. A segment is one start of the game from the save. A segment ends after a failed
destination, after a destination named in `last` when visits follow it, and when the game stops taking visits. Then the
game is quit and started again from the save, as the next segment. A failed destination is kept with its error and is
not retried.

The crawl file is `scenes/crawl.toml` under the harness root, outside git:

| Key | Meaning |
|---|---|
| `save` | required: the folder name of the save, under `scenes/saves` |
| `slot` | optional: two digits |
| `pan_frames` | required: presents of the full-right stick turn, at least 1 |
| `settle_frames` | required: presents waited after each arrival, at least 0 |
| `variants` | optional: a non-empty list of `child` and `adult`, each once (default both) |
| `last` | optional: distinct destination indexes, visited after the others, in this order |

Arguments:

- `--device D`: as for `hx bench`.
- `--build REV`: as for `hx calibrate`.

Records one run: `costmap.json` (an entry per destination and variant, with its segment, its map, its window's replayed
frames, its trace stats or the reason there are none, and any error), `metrics.json` (each segment's summary),
`crawl.toml` (the file's bytes), and, for each segment under `segments/<n>/`, `log.txt`, `hookshot_frames.csv`,
`frametimes.csv` and `kernel.txt` when it could be read. The cost map is also written to
`scenes/costmaps/<job id>.json`.

Prints `build: <sha>` and `job: <job id>`, then `destinations: N measured, M failed`, then `crawl: pass`, or one
`crawl: FAIL <problem>` line per problem, then `costmap: <path>`, `run: ID` and `manifest: <sha256>`.

**Exit status:**

- `0`: every destination was measured, and there is no problem.
- `1`: a destination failed, or the run has a problem. The run is recorded.
- `2`: the crawl could not run (a crawl file or build refused, a job or the game does not start), or the record is not
  written.
- `255`: ssh lost; the job goes on.

### `hx envbench`

```
hx envbench --device D --scene S --floor ID --b-env NAME=VALUE [--build REV]
```

Benches one build against itself, with environment pairs added to arm B's runs only, and judges the series against a
recorded floor. Both arms are the same build, as one bench job: the A-B-B-A timed runs and one correctness run per arm,
each run its own process through the build's closure, waiting for the hold before each. Each pair is added to arm B's
timed and correctness runs and to no arm A run. The floor must be a recorded floor of the same device, scene and method,
or the envbench is refused before any run. The series is judged as `hx bench` judges it, and it is void or failed on the
same conditions. No pass or fail is attached: the verdict call (faster, slower or no change) is the measurement.

Arguments:

- `--device D`, `--scene S`, `--floor ID`: as for `hx bench`.
- `--build REV`: as for `hx calibrate`.
- `--b-env NAME=VALUE`: an environment pair for arm B's runs only. Repeat the option for more pairs. Each
  `NAME` is given once: a name given twice is a usage error.

Records one run, with `envbench.json`: the outcome (`faster`, `slower`, `no change`, `void` or `failed`), the reason
or the void reasons, the pairs as `b_env`, the device, the scene, both shas, the suite's sha256, the method, the
floor's run id and its floor, whether the validation layer was used, the series (each run's
index, arm, kind, seconds held, run id and status), and the interval and each arm's medians when they were measured.

Prints `build A:` and `build B:` (each build's folder), the method's values, the number of runs in the plan, and
`job: <job id>`. Then it prints `envbench: ID`, then `verdict: <outcome> (interval L% to H%, floor F%)` with the outcome
`faster`, `slower` or `no change` (the interval's bounds signed); or `verdict: void (...)` with the reasons; or
`verdict: failed (...)` with the reason.

**Exit status:**

- `0`: a verdict (faster, slower or no change) is recorded.
- `1`: the envbench is void or failed, and recorded.
- `2`: the envbench could not run: an unknown device, scene or build; a floor that is refused; a job that will
  not start; or the envbench is not recorded.
- `255`: ssh lost; the job goes on.

### `hx floor`

```
hx floor --device D --scene S [--build REV]
```

Measures one device's noise floor for one scene, with one build in both arms. The method's plan runs as one bench job:
the A-B-B-A timed runs, then one correctness run per arm, which takes no screenshots and runs the validation layer when
the host's Vulkan lists it. Each run is a record run of its own. The series waits for the hold before each run and stops
at the first run that does not pass. Nothing is rerun.

Arguments:

- `--device D`, `--scene S`, `--build REV`: as for `hx calibrate`.

Records one run, with `floor.json`: the floor and its interval, each arm's run medians, whether the validation layer was
used, the series (each run's index, arm, kind, seconds held, run id and
status), the method, both shas and the suite's sha256. The run's facts are the first run's. A floor that fails is
recorded with outcome `failed` and its reason.

Prints `build A:` and `build B:` (each build's folder), the method's values, the number of runs in the plan, and
`job: <job id>`. Then it prints `floor: ID`, then `noise floor X% (interval Y% to Z%)` and `floor: recorded`. A failed
floor prints `floor: FAIL <reason>` instead.

**Exit status:**

- `0`: the floor is recorded.
- `1`: the floor failed, and is recorded.
- `2`: the floor could not run (an unknown device, scene or build; a suite or method that is refused; a job that will not
  start), or the record is not written.
- `255`: ssh lost; the job goes on.

### `hx mangohud`

```
hx mangohud [--root ROOT] csv run
```

Places a MangoHud log beside a recorded run, as a new record run that names it. The target run is not changed: a record
run is written once. `hx report` reads the new run like any other.

Arguments:

- `csv`: the MangoHud log, a CSV file that must be readable and not empty.
- `run`: the six-digit id of the run the log belongs to. Its manifest must read back.
- `--root ROOT`: the record root. Default `~/hookshot-harness/results` on the machine it runs on: run it on the build
  host (see [Running it](#running-it)).

Records a new run with `mangohud.csv` (the log's bytes) and `mangohud.json`: its kind (`mangohud`), the target run's id,
and the CSV's sha256 and size. The new run carries the target's facts.

Prints `run: ID`.

**Exit status:**

- `0`: the run is written.
- `2`: refused, with the cause on stderr and nothing written: a run id that is not six digits or whose manifest cannot
  be read, a CSV that is missing, unreadable or empty, or a usage error.

### `hx opening`

```
hx opening --device D [--build REV]
```

Takes a new game, with no save and its opening skipped, through the opening's steps, and keeps the save the game makes.
The steps are in `scenes/opening.toml` under the harness root, outside git:

- `save`: the folder name the save is kept under, in `scenes/saves`.
- `steps`: a list of tables. Each table has exactly one of `inputs` (a bridge input script), `wait` (presents to wait,
  at least 1) or `state` (one of `boot`, `intro`, `menu`, `loading`, `play`, `unknown`).

After the steps, the check waits to be in play with no event or load, confirms the device, and finds the save slot this
run wrote. A pass copies the game's save folder to `scenes/saves/<save>`. A folder that already exists is never
overwritten, and the check is refused before the game starts.

Arguments:

- `--device D`, `--build REV`: as for `hx calibrate`.

Records one run, once the game has started, whatever the verdict: `log.txt`, `hookshot_frames.csv`, `frametimes.csv`,
`kernel.txt` when it could be read, `metrics.json`, `opening.toml` (its bytes) and `opening.json` (the save, the slot,
the verdict, the reason and the exit status).

Prints `build: <sha>`, `job: <job id>`, `device: <name>`, `run: ID` and `manifest: <sha256>`, then
`opening: pass: slot <slot> saved to <path>` or `opening: FAIL <reason>`.

**Exit status:**

- `0`: pass.
- `1`: FAIL, and recorded.
- `2`: the check could not run (an opening file or save folder that is refused, a game that does not start, a job that
  will not start), or the run is not recorded.
- `255`: ssh lost; the job goes on.

### `hx report`

```
hx report [--root ROOT] id
```

Prints one run from the record, and only from the record. Each file and screenshot the run's manifest names is read back
and checked against its recorded sha256 and size before anything is printed. A changed or missing file refuses the whole
report, and stderr names its path.

Arguments:

- `id`: the run's six-digit id.
- `--root ROOT`: the record root. Default `~/hookshot-harness/results` on the machine it runs on: run it on the build
  host (see [Running it](#running-it)).

Prints, in order: `run ID`, `written <time>`, `manifest <sha256 of manifest.json>`, `prior <id> <sha256>` or
`prior none`, `facts` with one `key: value` line per fact, `files` with one `sha256 size path` line per file, and
`screenshots` with one `sha256 size name` line per screenshot.

**Exit status:**

- `0`: the run is printed.
- `1`: refused, with the cause on stderr: not a run id, a manifest that cannot be read or is of another shape, or a file
  that changed or is missing.
- `2`: a usage error.

### `hx run`

```
hx run --device D [--build REV] [--final] SCENE
```

Runs one scene of the suite once, as a bench job on device D, and records it. The scene is selected from
`scenes/suite.toml` under the harness root. A held-out scene runs only with `--final`. A scene marked warm runs from the
build's warm copy of the game for this device, which must already exist. A passing cold run of the build makes that copy
when it does not exist yet.

The run plays the scene's route, then holds its window: the scene's `seconds` at 60 game frames a second, counted by the
game's frame count and not the clock, so a slower build holds as many frames as a faster one. A scene with an `hour`
sets it after its route, and a load scene sets it after its arrival; either way the hour is set before the window.

Arguments:

- `SCENE`: a scene of the suite.
- `--device D`, `--build REV`: as for `hx calibrate`.
- `--final`: allow a held-out scene.

Records one run: `log.txt`, `hookshot_frames.csv`, `frametimes.csv`, `kernel.txt` when it could be read, `metrics.json`
(the summary and the window), `scene.json` (the scene's fields, the device, the final flag, the suite's sha256, the
window's map and bounds, the exit status, the verdict and the reason) and `suite.toml` (the suite file's bytes). Its
facts include the suite's sha256.

Prints `build: <sha>`, `job: <job id>`, `device: <name>`, `run: ID`, `manifest: <sha256>` and `suite: <sha256>`, then
`hx run: pass` or `hx run: FAIL <reason>`. A passing run whose warm copy cannot be made still passes, and a warning goes
to stderr.

**Exit status:**

- `0`: pass.
- `1`: FAIL, and recorded.
- `2`: the run could not run: an unknown device or scene; a held-out scene without `--final`; a suite or build that is
  missing or refused; a warm scene with no warm copy; a job or the game that does not start; or the run is not recorded.
- `255`: ssh lost; the job goes on.

### `hx smoke`

```
hx smoke --device D [--build REV] [--frames FRAMES]
```

Runs a build's game headless on device D until it has presented `FRAMES` presents. It passes when the renderer is the
device's Vulkan device on the native path, the game reached `FRAMES` presents, it replayed frames, and its log shows no
fallback to another GPU. It quits the game afterwards. It writes no record: the job's output stays in its job folder on
the host.

Arguments:

- `--device D`, `--build REV`: as for `hx calibrate`.
- `--frames FRAMES`: presents to wait for, a whole number of at least 1. Default 300.

Prints `build: <sha>` and `job: <job id>`, then `device: <renderer>` and `presents: <n>` once the game has answered,
then `smoke: pass` or `smoke: FAIL <reason>`.

**Exit status:**

- `0`: pass.
- `1`: FAIL, with the reason (a game that does not launch included).
- `2`: the check could not run: an unknown device; a build that is missing or refused; a job that will not start.
- `255`: ssh lost; the job goes on.

### `hx standing`

```
hx standing --device D --scene S A B
```

Runs the A-B-B-A timed runs of two builds as one bench job, each run under a driver with no control bridge, and
records the interval of B against A as unconfirmed. Each run starts a fresh copy of its build's game from the scene's
save and slot. The driver reads the game's status lines from its log, presses A through the game's live-input file
until the game is in play, waits for the settle, holds the scene's window of frames, and ends the game's process group.
The scene's route is not played. The client starts one bench job. Its in-job half runs each timed run as its own
process through its arm's closure, with `--run-in-job`, the in-job half of one run. Each run waits for the hold before
it starts, and the series stops at the first run that does not pass; nothing is rerun. Agents never call either
in-job half.

Arguments:

- `A`, `B`: the commits of the two builds, as for `hx bench`.
- `--device D`, `--scene S`: as for `hx bench`.

Records, for each timed run, a record run with `standing_run.json` (the device, the scene, the suite's sha256, the
presses sent, the presents at play, the window's first and last frame, the exit status, and the run's check: `pass`,
or `FAIL` with its reason), `log.txt`, `hookshot_frames.csv` and `kernel.txt` when it could be read. The check passes
when the log has the device's Vulkan device line, shows no fallback to another GPU, and the frame trace has the
window's frames. A run whose game does not start has no record run. The comparison is a record run of its own,
`standing.json`: it is marked `unconfirmed`, and it carries no faster, slower, pass or fail call. It holds the reason
when the series ended early, the device, the scene, both shas, the suite's sha256, the method, the series (each run's
index, arm, kind, seconds held, run id and status), and the interval of B against A and each arm's warm medians per
run, when they were measured.

Prints `build A:` and `build B:` (each build's folder), the method's values, the number of timed runs in the plan, and
`job: <job id>`. Then, for each timed run, `device: D`, `run: ID` and `standing run: pass` or
`standing run: FAIL <reason>`. Last, it prints `standing: ID`, then either
`interval of B against A L% to H% (unconfirmed)`, with the bounds signed, when every run passed, or
`standing: the series ended early: <reason>`.

**Exit status:**

- `0`: the comparison is recorded, and every run passed.
- `1`: a run did not pass (a game that does not start included), or a run's folder could not be made, or its
  frames could not be read back. The comparison is recorded, and the series ends there.
- `2`: the standing could not run: an unknown device, scene or build; a suite or method that is refused; a job that will
  not start; or the comparison is not recorded.
- `255`: ssh lost; the job goes on.

### `hx stop`

```
hx stop <job id>...
```

Ends each named job's unit on the host. Stopping the unit ends every process in it, the game included. All the named
units are stopped in one ssh command, and then each one is checked. The ids are checked before anything reaches the host.

Prints `stopped hx-<job id>.service` for each unit that is stopped. For each unit that is not, it prints
`still <state>: hx-<job id>.service`, with `no answer` where systemd gives none.

**Exit status:**

- `0`: every named job is stopped.
- `1`: a job is not stopped, or the configuration or the host cannot be used.
- `2`: no ids, or a malformed id. Nothing reaches the host.

### `hx verify`

```
hx verify [--root ROOT] [--journal JOURNAL]
```

Checks the whole record. It checks each run's manifest; each run's files and screenshots against their sha256 and size;
the run folder for files its manifest does not name; each run's `prior` against the run before it; and the journal. The
journal must hold each run's manifest hash on that run's line, and must not name a run the record lacks. It only reads.

Arguments:

- `--root ROOT`: the record root. Default `~/hookshot-harness/results` on the machine it runs on: run it on the build
  host (see [Running it](#running-it)).
- `--journal JOURNAL`: a file of `journalctl --user -t hookshot-harness -o cat` output, read instead of the user's
  journal.

Prints one line per problem, each starting with the run or the path it names. With no problem, it prints
`N runs checked, no problems`.

**Exit status:**

- `0`: no problem.
- `1`: problems, printed.
- `2`: the root is not a folder, the journal cannot be read (named on stderr), or a usage error.
