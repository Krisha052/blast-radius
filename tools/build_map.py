"""Build the coverage / fixture map from the pinned sqlglot commit.

Usage (inside the image, with `coverage` installed):
  python build_map.py <sqlglot checkout> <tests.json> <out.json>

Records, for every test: the sqlglot/tests source lines it executed (including
its class setup), and the non-Python files it opened or directories it listed.
Lines run while importing everything up front are recorded as "import" lines.
"""

import builtins
import io
import json
import os
import pkgutil
import sys
import time

ROOT = os.path.abspath(sys.argv[1])
TESTS = json.load(open(sys.argv[2]))["tests"]
OUT = os.path.abspath(sys.argv[3])
ONLY = set(sys.argv[4].split(",")) if len(sys.argv) > 4 else None

sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.chdir(ROOT)
os.environ["SKIP_INTEGRATION"] = "0"

import coverage  # noqa: E402

cov = coverage.Coverage(
    data_file=None,
    include=[ROOT + "/sqlglot/*", ROOT + "/tests/*"],
    config_file=False,
)
cov.start()
cov.switch_context("import")

import sqlglot  # noqa: E402

for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try:
        __import__(m.name)
    except BaseException as e:  # noqa: BLE001
        print("import fail", m.name, e, file=sys.stderr)

import testrun  # noqa: E402

files = sorted({t["file"] for t in TESTS})
for f in files:
    try:
        __import__(testrun.module_name(f))
    except BaseException as e:  # noqa: BLE001
        print("test import fail", f, e, file=sys.stderr)

current = ["import"]
reads = {}


def note(path):
    try:
        p = os.path.abspath(os.fspath(path))
    except TypeError:
        return
    if p.startswith(ROOT + "/") and not p.endswith(".pyc"):
        reads.setdefault(current[0], set()).add(os.path.relpath(p, ROOT))


_open, _io_open, _listdir, _scandir = builtins.open, io.open, os.listdir, os.scandir


def h_open(file, *a, **k):
    if isinstance(file, (str, bytes, os.PathLike)):
        note(file)
    return _open(file, *a, **k)


def h_listdir(path="."):
    note(path)
    return _listdir(path)


def h_scandir(path="."):
    note(path)
    return _scandir(path)


builtins.open = h_open
io.open = h_open
os.listdir = h_listdir
os.scandir = h_scandir


def on_class(f, cname):
    ctx = f"C:{f}::{cname}"
    current[0] = ctx
    cov.switch_context(ctx)


def on_test(num):
    ctx = f"T:{num}"
    current[0] = ctx
    cov.switch_context(ctx)


selected = [(t["number"], t["id"]) for t in TESTS if ONLY is None or t["file"] in ONLY]
t0 = time.time()
failed, durations = testrun.run(selected, on_class=on_class, on_test=on_test)
cov.stop()
print(f"ran {len(selected)} tests in {time.time() - t0:.1f}s, failed: {sorted(failed)}", file=sys.stderr)

data = cov.get_data()
lines = {}
for path in data.measured_files():
    rel = os.path.relpath(path, ROOT)
    by_line = data.contexts_by_lineno(path)
    lines[rel] = {str(ln): sorted(c for c in ctxs if c) for ln, ctxs in by_line.items()}

json.dump(
    {
        "lines": lines,
        "reads": {k: sorted(v) for k, v in reads.items()},
        "failed": sorted(failed),
        "durations": {str(k): round(v, 4) for k, v in durations.items()},
    },
    open(OUT, "w"),
)
print("wrote", OUT, file=sys.stderr)
