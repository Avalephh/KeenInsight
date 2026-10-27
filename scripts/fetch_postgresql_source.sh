#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="${POSTGRES_SOURCE_LOCK:-$PROJECT_ROOT/postgresql-source.lock}"
SOURCE_ROOT="${POSTGRES_SOURCE_ROOT:-$PROJECT_ROOT/third_party/postgresql-12.22}"
DOWNLOAD_DIR="${POSTGRES_SOURCE_DOWNLOAD_DIR:-$PROJECT_ROOT/third_party/downloads}"

for command_name in curl sha256sum tar; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "$command_name is required" >&2
    exit 1
  }
done
if [ ! -f "$LOCK_FILE" ]; then
  echo "PostgreSQL source lock file not found: $LOCK_FILE" >&2
  exit 1
fi

read -r source_name source_ref source_commit archive url sha256 < <(
  awk '$1 == "postgresql" {print; exit}' "$LOCK_FILE"
)
if [ "${source_name:-}" != "postgresql" ] || [ -z "${source_commit:-}" ] \
  || [ -z "${archive:-}" ] || [ -z "${url:-}" ] || [ -z "${sha256:-}" ]; then
  echo "invalid PostgreSQL source lock: $LOCK_FILE" >&2
  exit 1
fi

if [ -e "$SOURCE_ROOT" ]; then
  if [ -f "$SOURCE_ROOT/.gitrevision" ] \
    && [ "$(tr -d '[:space:]' < "$SOURCE_ROOT/.gitrevision")" = "$source_commit" ] \
    && [ -f "$SOURCE_ROOT/src/backend/utils/misc/guc.c" ]; then
    echo "accepted existing PostgreSQL source tree at $SOURCE_ROOT ($source_ref/$source_commit)"
    exit 0
  fi
  echo "refusing to overwrite existing PostgreSQL source directory: $SOURCE_ROOT" >&2
  echo "choose an empty destination with POSTGRES_SOURCE_ROOT or move the old snapshot manually" >&2
  exit 1
fi

mkdir -p "$DOWNLOAD_DIR"
archive_path="$DOWNLOAD_DIR/$archive"
if [ ! -f "$archive_path" ] \
  || ! printf '%s  %s\n' "$sha256" "$archive_path" | sha256sum -c - >/dev/null 2>&1; then
  echo "downloading PostgreSQL $source_ref" >&2
  curl -fL --retry 3 --connect-timeout 15 "$url" -o "$archive_path"
fi
printf '%s  %s\n' "$sha256" "$archive_path" | sha256sum -c - >/dev/null

work_dir="$(mktemp -d /tmp/keeninsight-postgresql.XXXXXX)"
trap 'rm -rf "$work_dir"' EXIT
tar -xjf "$archive_path" -C "$work_dir"
extracted="$work_dir/postgresql-12.22"
if [ ! -f "$extracted/src/backend/utils/misc/guc.c" ]; then
  echo "PostgreSQL archive has an unexpected layout: $archive_path" >&2
  exit 1
fi
mkdir -p "$(dirname -- "$SOURCE_ROOT")"
mv "$extracted" "$SOURCE_ROOT"
printf '%s\n' "$source_commit" >"$SOURCE_ROOT/.gitrevision"

echo "PostgreSQL source $source_ref/$source_commit is ready at $SOURCE_ROOT"
