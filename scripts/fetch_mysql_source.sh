#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="$PROJECT_ROOT/mysql-source.lock"
DESTINATION="${MYSQL_SOURCE_ROOT:-$PROJECT_ROOT/third_party/mysql-8.0.42}"
ARCHIVE_DIR="${MYSQL_SOURCE_ARCHIVE_DIR:-$PROJECT_ROOT/third_party/mysql-archives}"

read -r _source _version archive url expected < <(awk 'NF && $1 !~ /^#/ {print; exit}' "$LOCK")
if [ -z "${archive:-}" ] || [ -z "${url:-}" ] || [ -z "${expected:-}" ]; then
  echo "invalid mysql-source.lock" >&2
  exit 1
fi

if [ -d "$DESTINATION/sql" ] && [ -d "$DESTINATION/storage" ]; then
  echo "MySQL source already present: $DESTINATION"
  exit 0
fi

mkdir -p "$ARCHIVE_DIR"
archive_path="$ARCHIVE_DIR/$archive"
if [ ! -f "$archive_path" ]; then
  echo "downloading $url"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --max-time 1800 -o "$archive_path.part" "$url"
  else
    wget -O "$archive_path.part" "$url"
  fi
  mv "$archive_path.part" "$archive_path"
fi

actual="$(sha256sum "$archive_path" | awk '{print $1}')"
if [ "$actual" != "$expected" ]; then
  echo "checksum mismatch for $archive_path: $actual != $expected" >&2
  exit 1
fi

temporary="$(mktemp -d "$DESTINATION.tmp.XXXXXX")"
trap 'rm -rf "$temporary"' EXIT
tar -xzf "$archive_path" -C "$temporary" --strip-components=1
mkdir -p "$(dirname "$DESTINATION")"
rm -rf "$DESTINATION"
mv "$temporary" "$DESTINATION"
trap - EXIT
echo "MySQL source ready: $DESTINATION"
echo "Set MYSQL_SOURCE_ROOT=$DESTINATION for the bridge and function indexer."
