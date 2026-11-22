"""Safe fallback: run every test, with the worker count that minimises the busiest worker plus 2 s per worker."""
import json
import os
import sys

tests = json.load(open("/data/tests.json"))["tests"]
order = sorted(tests, key=lambda t: -t["seconds"])
best = None
for k in range(1, 9):
    loads, ws = [0] * k, [[] for _ in range(k)]
    for t in order:
        i = loads.index(min(loads))
        ws[i].append(t["number"])
        loads[i] += round(t["seconds"] * 1000)
    cost = max(loads) + 2000 * k
    if best is None or cost < best[0]:
        best = (cost, ws)
line = best[1]
for p in sorted(os.listdir("/data/commits")):
    if p.endswith(".patch"):
        print(json.dumps({"commit": p[:-6], "workers": line}))
print(f"all tests, {len(line)} workers, {best[0]/1000:.1f}s a commit", file=sys.stderr)
