"""Merge per-test-file maps into the compact runtime map.

  python merge_map.py <tests.json> <maps dir> <out map.json.gz>

Class-setup contexts expand to every test of that class. Identical test sets
are stored once.
"""

import glob
import gzip
import json
import sys

tests = json.load(open(sys.argv[1]))["tests"]
by_class = {}
for t in tests:
    f, cls, _ = t["id"].split("::")
    by_class.setdefault(f"{f}::{cls}", []).append(t["number"])


def expand(ctx):
    if ctx.startswith("T:"):
        return [int(ctx[2:])]
    if ctx.startswith("C:"):
        return by_class.get(ctx[2:], [])
    return []


lines = {}
reads = {}
imports = {}
failed = []
for path in sorted(glob.glob(sys.argv[2] + "/*.json")):
    m = json.load(open(path))
    failed += m["failed"]
    for f, d in m["lines"].items():
        fl = lines.setdefault(f, {})
        for ln, ctxs in d.items():
            s = fl.setdefault(int(ln), set())
            if "import" in ctxs:
                imports.setdefault(f, set()).add(int(ln))
            for c in ctxs:
                s.update(expand(c))
    for ctx, paths in m["reads"].items():
        for p in paths:
            reads.setdefault(p, set()).update(expand(ctx))

sets = []
index = {}


def sid(s):
    key = tuple(sorted(s))
    if key not in index:
        index[key] = len(sets)
        sets.append(list(key))
    return index[key]


out_lines = {}
filecov = {}
for f, d in sorted(lines.items()):
    od = {}
    allt = set()
    for ln, s in sorted(d.items()):
        if s:
            od[str(ln)] = sid(s)
            allt |= s
    if od:
        out_lines[f] = od
        filecov[f] = sid(allt)

out = {
    "sets": sets,
    "lines": out_lines,
    "filecov": filecov,
    "reads": {p: sid(s) for p, s in sorted(reads.items()) if s},
    "imp": {f: sorted(v) for f, v in sorted(imports.items())},
}
with gzip.open(sys.argv[3], "wt", compresslevel=9) as f:
    json.dump(out, f, separators=(",", ":"))
print(f"files {len(out_lines)} sets {len(sets)} reads {len(out['reads'])} failed-at-build {sorted(set(failed))}")
