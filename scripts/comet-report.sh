#!/usr/bin/env bash
# Comet report entrypoint — wraps generate_report.py for shell / comet-env integration.
set -euo pipefail

_script_source="${BASH_SOURCE[0]:-$0}"
_script_dir="$(cd "$(dirname "$_script_source")" && pwd -P)"
_generator="${_script_dir}/generate_report.py"

if [ ! -f "$_generator" ]; then
  echo "ERROR: generate_report.py not found beside comet-report.sh ($_generator)" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required to generate Comet reports." >&2
  exit 1
fi

exec python3 "$_generator" "$@"
