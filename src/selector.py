"""Blast Radius selector.

For every patch: work out which tests could notice it from a coverage map of
the pinned sqlglot commit, run those tests against the patched tree in a
forked child, and print only the tests that actually fail.
"""

import gzip
import json
import os
import select
import signal
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
BUDGET = float(os.environ.get("BR_BUDGET", "120"))  # wall seconds before new commits go blind
HARD = float(os.environ.get("BR_HARD", "160"))  # wall seconds before everything left is answered blind
CHILD_MAX = float(os.environ.get("BR_CHILD_MAX", "60"))  # wall seconds one commit may run
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
        self.imp = {f: set(v) for f, v in m.get("imp", {}).items()}

    def runs_at_import(self, f, lo, hi):
        lines = self.imp.get(f)
        return bool(lines) and any(ln in lines for ln in range(lo, hi + 1))

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
    """Returns (new file texts, candidate tests, groups, notes). Each group is
    the set of tests covering one changed site."""
    files = patchlib.parse(text)
    new_texts = {}
    groups = []
    notes = []
    for fp in files:
        old = read_root(fp.old) if fp.old else None
        if fp.binary:
            notes.append(f"binary {fp.path}")
            groups.append(mp.read_tests(fp.path))
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
            groups.extend(py_candidates(path, old, new, fp, mp, tests_by_file, notes))
        else:
            g = mp.read_tests(path)
            if fp.new and fp.new != path:
                g |= mp.read_tests(fp.new)
            groups.append(g)
    groups = [frozenset(g) for g in groups if g]
    cands = set().union(*groups) if groups else set()
    return new_texts, cands, groups, notes


def py_candidates(path, old, new, fp, mp, tests_by_file, notes):
    if old is not None and new is not None:
        ko = analyze.token_key(old)
        if ko is not None and ko == analyze.token_key(new):
            notes.append(f"comment-only {path}")
            return [mp.read_tests(path)]
    if old is None:
        notes.append(f"new file {path}")
        return []
    if new is None or not analyze.compiles(new, path):
        notes.append(f"broken {path}")
        return [module_level(path, mp, tests_by_file)]
    out = []
    seen = set()
    module_hit = False
    for kind, where in analyze.change_sites(old, fp):
        if kind == "func":
            if where in seen:
                continue
            seen.add(where)
            def_line, body_start, end = where
            out.append(mp.tests_for_lines(path, body_start, end))
            # A function that runs while modules import (metaclasses, table
            # builders) affects whoever uses the module, not just its coverers.
            if mp.runs_at_import(path, body_start, end):
                notes.append(f"import-time func {path}:{def_line}")
                module_hit = True
        elif kind == "module":
            module_hit = True
    if module_hit:
        notes.append(f"module-level {path}")
        out.append(module_level(path, mp, tests_by_file))
    return out


DIALECT_DIRS = ("dialects", "parsers", "generators", "tokenizers", "typing")


def module_level(path, mp, tests_by_file):
    out = mp.file_tests(path)
    parts = path.split("/")
    # Class-level settings of a dialect (e.g. WEEK_OFFSET) are read by shared
    # code, so take every test that touches any part of that dialect.
    if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in DIALECT_DIRS and parts[2] != "__init__.py":
        for d in DIALECT_DIRS:
            out |= mp.file_tests(f"sqlglot/{d}/{parts[2]}")
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


CHEAP = float(os.environ.get("BR_CHEAP", "1.0"))  # run anything cheaper than this
RUN_MAX = float(os.environ.get("BR_RUN_MAX", "4.0"))  # sole coverers up to this get run


def decide(cands, groups, cost, secs):
    """Split candidates into (run first, expensive). Cheap = cost below CHEAP."""
    cheap = {n for n in cands if cost[n] < CHEAP}
    return cheap, cands - cheap


def resolve_expensive(exp, groups, cheap, failed, cost):
    """After the cheap run: which expensive tests to run, include blind, or skip."""
    run, include = set(), set()
    for e in sorted(exp):
        mine = [g for g in groups if e in g]
        sole = any(not (g & cheap) for g in mine)
        hit = any(g & failed for g in mine)
        if hit:
            include.add(e)
        elif cost[e] <= RUN_MAX:
            run.add(e)
        elif sole:
            include.add(e)
    return run, include


def child_run(overlay, cheap, exp, groups, ids, cost, timeouts, wfd):
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    os.dup2(2, 1)
    os.chdir(overlay)
    sys.path[:] = [p for p in sys.path if not p.startswith(ROOT)]
    sys.path.insert(0, overlay)
    import testrun

    def order(ns):
        return [(n, ids[n]) for n in sorted(ns, key=lambda n: (ids[n].split("::")[0], n))]

    tf = lambda n: timeouts.get(n, 0)  # noqa: E731
    failed, durations = testrun.run(order(cheap), timeout_for=tf)
    run, include = resolve_expensive(exp, groups, cheap, failed, cost)
    if run:
        f2, d2 = testrun.run(order(run), timeout_for=tf)
        failed |= f2
        durations.update(d2)
    failed |= include
    os.write(wfd, json.dumps({"failed": sorted(failed), "dur": durations,
                              "ran": len(cheap) + len(run), "blind": sorted(include)}).encode())
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
    cost = dict(secs)
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "costs.json")) as f:
            cj = json.load(f)
        cost.update({int(k): v for k, v in cj["cost"].items()})
    except (OSError, ValueError, KeyError):
        pass

    commits = sorted(p[:-6] for p in os.listdir(os.path.join(DATA, "commits")) if p.endswith(".patch"))
    jobs = []
    for c in commits:
        with open(os.path.join(DATA, "commits", c + ".patch"), encoding="utf-8", errors="replace") as f:
            text = f.read()
        try:
            new_texts, cands, groups, notes = plan(c, text, mp, tests_by_file)
        except Exception as e:  # noqa: BLE001
            log(f"{c}: plan failed {e!r}; running everything blind")
            new_texts, cands, groups, notes = None, set(secs), [], ["plan failed"]
        log(f"{c}: {len(cands)} candidates, {sum(secs[n] for n in cands):.2f}s  {notes}")
        jobs.append((c, new_texts, cands, groups))

    emitted = set()

    def emit(c, nums):
        if c in emitted:
            return
        emitted.add(c)
        out.write(json.dumps({"commit": c, "workers": split(sorted(nums), secs)}) + "\n")
        out.flush()

    def bail(*_):
        # Out of time: answer every commit left with all its candidates, then stop.
        for c, new_texts, cands, groups in jobs:
            emit(c, cands)
        out.flush()
        log(f"hard deadline at {time.time() - T0:.1f}s; answered the rest blind")
        os._exit(0)

    signal.signal(signal.SIGALRM, bail)
    signal.setitimer(signal.ITIMER_REAL, max(1.0, HARD - (time.time() - T0)))

    for c, new_texts, cands, groups in jobs:
        if not cands:
            emit(c, ())

    if os.environ.get("BR_PLAN_ONLY"):
        for c, new_texts, cands, groups in jobs:
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
            c, new_texts, cands, groups = pending.pop(0)
            if new_texts is None or time.time() - T0 > BUDGET:
                log(f"{c}: blind include")
                emit(c, cands)
                continue
            overlay = os.path.join(TMP, c)
            patchlib.make_overlay(ROOT, overlay, new_texts)
            cheap, exp = decide(cands, groups, cost, secs)
            timeouts = {n: max(10.0, secs[n] * 6 + 5) for n in cands}
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(r)
                    child_run(overlay, cheap, exp, groups, ids, cost, timeouts, w)
                finally:
                    os._exit(0)
            os.close(w)
            running[pid] = [c, r, overlay, cands, b"", time.time()]
        if not running:
            continue
        now = time.time()
        for pid, v in list(running.items()):
            if now - v[5] > CHILD_MAX:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
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
                slow = sorted(((float(d), int(n)) for n, d in res["dur"].items()), reverse=True)[:3]
                log(f"{c}: ran {res['ran']}, blind {res['blind']}, slowest "
                    + ", ".join(f"{ids[n].split('::')[-1]} {d:.1f}s (rec {secs[n]})" for d, n in slow))
            except Exception:  # noqa: BLE001
                log(f"{c}: child died (status {status}); including all candidates")
                failed = cands
            log(f"{c}: {len(cands)} candidates, {time.time() - started:.1f}s -> failing {sorted(failed)}")
            emit(c, failed)
            shutil.rmtree(overlay, ignore_errors=True)

    signal.setitimer(signal.ITIMER_REAL, 0)
    for c, new_texts, cands, groups in jobs:
        emit(c, cands)
    t = os.times()
    log(f"done in {time.time() - T0:.1f}s wall, cpu {t.user + t.system + t.children_user + t.children_system:.1f}s")


if __name__ == "__main__":
    main()
