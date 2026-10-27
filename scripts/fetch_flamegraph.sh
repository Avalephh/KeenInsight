#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
destination="${FLAMEGRAPH_SOURCE_ROOT:-$root/app/vendor/FlameGraph}"
revision=41fee1f99f9276008b7cd112fca19dc3ea84ac32
if [ -e "$destination" ]; then
  if [ -d "$destination/.git" ] && [ "$(git -C "$destination" rev-parse HEAD)" = "$revision" ]; then
    echo "Accepted pinned FlameGraph checkout"; exit 0
  fi
  echo "Refusing to overwrite $destination" >&2; exit 1
fi
git init --quiet "$destination"
git -C "$destination" remote add origin https://github.com/brendangregg/FlameGraph.git
git -C "$destination" fetch --quiet --depth 1 origin "$revision"
git -C "$destination" checkout --quiet --detach FETCH_HEAD
test "$(git -C "$destination" rev-parse HEAD)" = "$revision"
test -f "$destination/stackcollapse-perf.pl"
echo "Pinned FlameGraph downloaded with its upstream license."
