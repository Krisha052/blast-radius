#!/bin/sh
# Run the native-arch image on a data dir under the scorer's limits, then score.
# usage: tools/run_local.sh [datadir=sample] [extra docker args...]
D=${1:-sample}; shift 2>/dev/null
docker run --rm --network none --cpus 2 --memory 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --user 65534:65534 -e HOME=/tmp "$@" \
  -v "$PWD/$D/commits:/data/commits:ro" -v "$PWD/$D/tests.json:/data/tests.json:ro" br-arm > output.jsonl 2> work/run.log
grep -E "done in|hard deadline|Traceback" work/run.log
[ -f "$D/answers.json" ] && python3 score.py "$D" "$D/answers.json" output.jsonl | head -2
