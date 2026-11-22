"""Run sqlglot unittest tests in-process, the way pytest would judge them.

A test fails when it errors, fails, has a failing subtest, its class setup
fails, or its module cannot be imported. Shared by the map builder and the
runtime selector so both see tests the same way.
"""

import importlib
import signal
import sys
import time
import unittest
from concurrent.futures import Future


class SyncExecutor:
    """Stands in for the optimizer tests' spawn pool: same results, no processes."""

    def __init__(self, *a, **k):
        pass

    def submit(self, fn, *args, **kwargs):
        fut = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.shutdown()


class Timeout(Exception):
    pass


class _Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.bad = False

    def addError(self, test, err):
        self.bad = True

    def addFailure(self, test, err):
        self.bad = True

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.bad = True

    def addUnexpectedSuccess(self, test):
        self.bad = True


def _alarm(signum, frame):
    raise Timeout()


def module_name(path):
    return path[:-3].replace("/", ".")


def run(tests, on_class=None, on_test=None, timeout_for=None):
    """tests: list of (number, id). Returns (failed set, {number: seconds})."""
    by_file = {}
    for num, tid in tests:
        f, cls, meth = tid.split("::")
        by_file.setdefault(f, {}).setdefault(cls, []).append((num, meth))

    failed = set()
    durations = {}
    old = signal.signal(signal.SIGALRM, _alarm)
    try:
        for f, classes in by_file.items():
            try:
                mod = importlib.import_module(module_name(f))
                if hasattr(mod, "ProcessPoolExecutor"):
                    mod.ProcessPoolExecutor = SyncExecutor
            except BaseException:  # noqa: BLE001
                for items in classes.values():
                    failed.update(n for n, _ in items)
                continue
            for cname, items in classes.items():
                cls = getattr(mod, cname, None)
                if cls is None:
                    failed.update(n for n, _ in items)
                    continue
                if getattr(cls, "__unittest_skip__", False):
                    continue
                if on_class:
                    on_class(f, cname)
                try:
                    cls.setUpClass()
                except BaseException:  # noqa: BLE001
                    failed.update(n for n, _ in items)
                    try:
                        cls.doClassCleanups()
                    except BaseException:  # noqa: BLE001
                        pass
                    continue
                for num, meth in items:
                    if on_test:
                        on_test(num)
                    res = _Result()
                    t0 = time.perf_counter()
                    limit = timeout_for(num) if timeout_for else 0
                    if limit:
                        signal.setitimer(signal.ITIMER_REAL, limit)
                    try:
                        cls(meth).run(res)
                    except BaseException:  # noqa: BLE001
                        res.bad = True
                    finally:
                        if limit:
                            signal.setitimer(signal.ITIMER_REAL, 0)
                    durations[num] = time.perf_counter() - t0
                    if res.bad:
                        failed.add(num)
                try:
                    cls.tearDownClass()
                    cls.doClassCleanups()
                except BaseException:  # noqa: BLE001
                    pass
    finally:
        signal.signal(signal.SIGALRM, old)
    return failed, durations
