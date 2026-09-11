#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="${KEENINSIGHT_SOURCE_LOCK:-$PROJECT_ROOT/external-sources.lock}"
SOURCE_ROOT="${SYSINSIGHT_REPOSITORY_ROOT:-$PROJECT_ROOT/repositories/Avalephh-KeenInsight/branch-sources}"
UPSTREAM_URL="${KEENINSIGHT_UPSTREAM_URL:-https://github.com/Avalephh/KeenInsight.git}"

if ! command -v git >/dev/null 2>&1; then
  echo "git is required" >&2
  exit 1
fi
if [ ! -f "$LOCK_FILE" ]; then
  echo "source lock file not found: $LOCK_FILE" >&2
  exit 1
fi

mkdir -p "$SOURCE_ROOT"

while read -r branch commit; do
  [ -z "${branch:-}" ] && continue
  case "$branch" in
    \#*) continue ;;
  esac
  if [ -z "${commit:-}" ]; then
    echo "invalid lock entry for $branch" >&2
    exit 1
  fi

  destination="$SOURCE_ROOT/$branch"
  if [ -e "$destination" ] && [ ! -d "$destination/.git" ]; then
    echo "refusing to overwrite non-Git source directory: $destination" >&2
    echo "choose an empty destination with SYSINSIGHT_REPOSITORY_ROOT or move the old snapshot manually" >&2
    exit 1
  fi

  if [ ! -d "$destination/.git" ]; then
    git clone --quiet --filter=blob:none --depth 1 --branch "$branch" --single-branch \
      "$UPSTREAM_URL" "$destination"
  fi

  if ! git -C "$destination" cat-file -e "$commit^{commit}" 2>/dev/null; then
    git -C "$destination" fetch --quiet origin "$commit"
  fi
  git -C "$destination" checkout --quiet --detach "$commit"

  actual="$(git -C "$destination" rev-parse HEAD)"
  if [ "$actual" != "$commit" ]; then
    echo "$branch resolved to $actual, expected $commit" >&2
    exit 1
  fi
  echo "$branch: $actual ($destination)"
done < "$LOCK_FILE"

echo "KeenInsight source snapshots are ready under $SOURCE_ROOT"
