#!/bin/sh
# Run inside the image: builds one map per test file into work/maps/.
pip install -q coverage==7.10.7 2>/dev/null
cp -r /opt/sqlglot /tmp/s
for f in $(python -c "import json;print(' '.join(sorted({t['file'] for t in json.load(open('sample/tests.json'))['tests']})))"); do
  out=work/maps/$(echo $f | tr '/' '_').json
  [ -f $out ] && continue
  echo "== $f" >&2
  python tools/build_map.py /tmp/s sample/tests.json $out $f 2>&1 | grep -v "^import fail" | tail -2 >&2
done
