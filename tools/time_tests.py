"""Measure in-process CPU cost of every test and class setup on the pristine tree.
  python time_tests.py <sqlglot> <tests.json> <out.json>   (one process per test file)"""
import json, os, subprocess, sys
root, tj, out = sys.argv[1:4]
tests = json.load(open(tj))["tests"]
files = sorted({t["file"] for t in tests})
res = {"test": {}, "setup": {}, "import": {}}
here = os.path.dirname(os.path.abspath(__file__))
for f in files:
    sel = [(t["number"], t["id"]) for t in tests if t["file"] == f]
    code = f"""
import sys, json, time, os
sys.path[:0] = [{root!r}, {os.path.join(here, '..', 'src')!r}]
os.chdir({root!r})
import duckdb, pandas, pandas.testing, numpy
def cpu(): t = os.times(); return t.user + t.system
c = cpu(); import sqlglot, testrun, importlib; importlib.import_module(testrun.module_name({f!r})); imp = cpu() - c
st = {{}}
failed, dur = testrun.run({sel!r}, setup_times=st)
print(json.dumps({{"imp": imp, "dur": dur, "st": st, "failed": sorted(failed)}}))
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**os.environ, "SKIP_INTEGRATION": "0"})
    d = json.loads(r.stdout.strip().splitlines()[-1])
    res["import"][f] = d["imp"]
    res["test"].update(d["dur"])
    res["setup"].update(d["st"])
    print(f, f"{sum(d['dur'].values()):.1f}s", d["failed"], file=sys.stderr)
json.dump(res, open(out, "w"))
