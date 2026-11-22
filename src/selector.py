"""Blast Radius selector.

For every patch: work out which tests could notice it from a coverage map of
the pinned sqlglot commit, run those tests against the patched tree in a
forked child, and print only the tests that actually fail.
"""

import gzip
import json
import os
import select
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyze  # noqa: E402
import patchlib  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
ROOT = os.environ.get("BR_ROOT", "/opt/sqlglot")
MAP = os.environ.get("BR_MAP", os.path.join(os.path.dirname(os.path.abspath(__file__)), "map.json.gz"))
TMP = os.environ.get("BR_TMP", "/tmp/br")
PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
BUDGET = float(os.environ.get("BR_BUDGET", "150"))  # wall seconds before falling back
WORKER_COST = 2.0
T0 = time.time()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Map:
    def __init__(self, path):
        with gzip.open(path, "rt") as f:
            m = json.load(f)
        self.sets = [frozenset(s) for s in m["sets"]]
        self.lines = {f: {int(k): v for k, v in d.items()} for f, d in m["lines"].items()}
        self.reads = m["reads"]
        self.filecov = m["filecov"]

    def tests_for_lines(self, f, lo, hi):
        d = self.lines.get(f)
        out = set()
        if not d:
            return out
        for ln in range(lo, hi + 1):
            sid = d.get(ln)
            if sid is not None:
                out |= self.sets[sid]
        return out

    def file_tests(self, f):
        sid = self.filecov.get(f)
        return set(self.sets[sid]) if sid is not None else set()

    def read_tests(self, path):
        out = set()
        sid = self.reads.get(path)
        if sid is not None:
            out |= self.sets[sid]
        d = os.path.dirname(path)
        while d:
            sid = self.reads.get(d)
            if sid is not None:
                out |= self.sets[sid]
            d = os.path.dirname(d)
        return out


def read_root(rel):
    try:
        with open(os.path.join(ROOT, rel), encoding="utf-8", newline="") as f:
            return f.read()
    except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
        return None


def plan(commit, text, mp, tests_by_file):
    """Returns (new file texts, candidate tests, notes)."""
    files = patchlib.parse(text)
    new_texts = {}
    cands = set()
    notes = []
    for fp in files:
        old = read_root(fp.old) if fp.old else None
        if fp.binary:
            notes.append(f"binary {fp.path}")
            cands |= mp.read_tests(fp.path)
            continue
        try:
            new = patchlib.apply(old, fp)
        except ValueError as e:
            notes.append(str(e))
            new = old
        if fp.old and fp.new and fp.old != fp.new:
            new_texts[fp.old] = None
        new_texts[fp.new or fp.old] = new
        path = fp.old or fp.new
        if path.endswith(".py"):
            cands |= py_candidates(path, old, new, fp, mp, tests_by_file, notes)
        else:
            cands |= mp.read_tests(path)
            if fp.new and fp.new != path:
                cands |= mp.read_tests(fp.new)
    return new_texts, cands, notes


def py_candidates(path, old, new, fp, mp, tests_by_file, notes):
    if old is not None and new is not None:
        ko = analyze.token_key(old)
        if ko is not None and ko == analyze.token_key(new):
            notes.append(f"comment-only {path}")
            return set()
    out = set()
    if old is None:
        notes.append(f"new file {path}")
        return out
    if new is None or not analyze.compiles(new, path):
        notes.append(f"broken {path}")
        return module_level(path, mp, tests_by_file)
    sites = analyze.change_sites(old, fp)
    module_hit = False
    for kind, where in sites:
        if kind == "func":
            def_line, body_start, end = where
            out |= mp.tests_for_lines(path, body_start, end)
        elif kind == "module":
            module_hit = True
    if module_hit:
        notes.append(f"module-level {path}")
        out |= module_level(path, mp, tests_by_file)
    return out


def module_level(path, mp, tests_by_file):
    out = mp.file_tests(path)
    if path.startswith("tests/"):
        out |= set(tests_by_file.get(path, ()))
    return out


def split(nums, secs, max_workers=8):
    """Best worker count and LPT split for the given tests."""
    if not nums:
        return []
    order = sorted(nums, key=lambda n: -secs[n])
    best = None
    for k in range(1, min(max_workers, len(order)) + 1):
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for n in order:
            i = loads.index(min(loads))
            ws[i].append(n)
            loads[i] += round(secs[n] * 1000)
        cost = max(loads) / 1000 + WORKER_COST * k
        if best is None or cost < best[0] - 1e-9:
            best = (cost, ws)
    return best[1]


def child_run(overlay, tests, timeouts, wfd):
    os.dup2(2, 1)
    os.chdir(overlay)
    sys.path[:] = [p for p in sys.path if not p.startswith(ROOT)]
    sys.path.insert(0, overlay)
    import testrun

    failed, durations = testrun.run(tests, timeout_for=lambda n: timeouts.get(n, 0))
    os.write(wfd, json.dumps({"failed": sorted(failed), "dur": durations}).encode())
    os.close(wfd)


def preload():
    for m in ("duckdb", "pandas", "pandas.testing", "numpy", "dateutil", "pytz", "setuptools",
              "setuptools.command.build_ext", "unittest.mock", "concurrent.futures", "csv", "gzip",
              "tracemalloc", "multiprocessing", "decimal", "datetime", "json", "tempfile", "runpy"):
        try:
            __import__(m)
        except BaseException:  # noqa: BLE001
            pass


def main():
    out = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)
    os.environ.setdefault("SKIP_INTEGRATION", "0")

    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    secs = {t["number"]: t["seconds"] for t in tests}
    ids = {t["number"]: t["id"] for t in tests}
    tests_by_file = {}
    for t in tests:
        tests_by_file.setdefault(t["file"], []).append(t["number"])
    mp = Map(MAP)

    commits = sorted(p[:-6] for p in os.listdir(os.path.join(DATA, "commits")) if p.endswith(".patch"))
    jobs = []
    for c in commits:
        with open(os.path.join(DATA, "commits", c + ".patch"), encoding="utf-8", errors="replace") as f:
            text = f.read()
        try:
            new_texts, cands, notes = plan(c, text, mp, tests_by_file)
        except Exception as e:  # noqa: BLE001
            log(f"{c}: plan failed {e!r}; running everything blind")
            new_texts, cands, notes = None, set(secs), ["plan failed"]
        log(f"{c}: {len(cands)} candidates, {sum(secs[n] for n in cands):.2f}s  {notes}")
        jobs.append((c, new_texts, cands))

    def emit(c, nums):
        out.write(json.dumps({"commit": c, "workers": split(sorted(nums), secs)}) + "\n")
        out.flush()

    for c, new_texts, cands in jobs:
        if not cands:
            emit(c, ())

    if os.environ.get("BR_PLAN_ONLY"):
        for c, new_texts, cands in jobs:
            if cands:
                emit(c, cands)
        return

    preload()
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    pending = [j for j in jobs if j[2]]
    running = {}  # pid -> [commit, rfd, overlay, cands, buf, started]
    while pending or running:
        while pending and len(running) < PARALLEL:
            c, new_texts, cands = pending.pop(0)
            if new_texts is None or time.time() - T0 > BUDGET:
                log(f"{c}: blind include")
                emit(c, cands)
                continue
            overlay = os.path.join(TMP, c)
            patchlib.make_overlay(ROOT, overlay, new_texts)
            order = sorted(cands, key=lambda n: (ids[n].split("::")[0], n))
            todo = [(n, ids[n]) for n in order]
            timeouts = {n: max(10.0, secs[n] * 6 + 5) for n in cands}
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(r)
                    child_run(overlay, todo, timeouts, w)
                finally:
                    os._exit(0)
            os.close(w)
            running[pid] = [c, r, overlay, cands, b"", time.time()]
        if not running:
            continue
        fds = {v[1]: pid for pid, v in running.items()}
        ready, _, _ = select.select(list(fds), [], [], 1.0)
        for fd in ready:
            pid = fds[fd]
            chunk = os.read(fd, 1 << 16)
            if chunk:
                running[pid][4] += chunk
                continue
            c, r, overlay, cands, buf, started = running.pop(pid)
            os.close(r)
            _, status = os.waitpid(pid, 0)
            try:
                res = json.loads(buf.decode())
                failed = set(res["failed"]) & cands
            except Exception:  # noqa: BLE001
                log(f"{c}: child died (status {status}); including all candidates")
                failed = cands
            log(f"{c}: ran {len(cands)} in {time.time() - started:.1f}s -> failing {sorted(failed)}")
            emit(c, failed)
            shutil.rmtree(overlay, ignore_errors=True)

    t = os.times()
    log(f"done in {time.time() - T0:.1f}s wall, cpu {t.user + t.system + t.children_user + t.children_system:.1f}s")


if __name__ == "__main__":
    main()
