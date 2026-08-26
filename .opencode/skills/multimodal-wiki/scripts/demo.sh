#!/usr/bin/env bash
set -euo pipefail

skill_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${skill_dir}/../../../.." && pwd)"
cd "${project_root}"

command_name="${1:-preflight}"
case "${command_name}" in
  start)
    exec python3 tools/opencode_demo.py start
    ;;
  status)
    exec python3 tools/opencode_demo.py status
    ;;
  tour)
    exec python3 tools/opencode_demo.py tour
    ;;
  compare)
    exec python3 tools/opencode_demo.py compare
    ;;
  questions)
    exec python3 tools/opencode_demo.py questions
    ;;
  table|visual|refuse)
    exec python3 tools/opencode_demo.py live --case "${command_name}"
    ;;
  preflight)
    exec python3 tools/demo_preflight.py
    ;;
  serve)
    exec python3 app.py api --host 127.0.0.1 --port 19828
    ;;
  query)
    shift
    if [[ $# -lt 1 ]]; then
      echo 'usage: demo.sh query <question> [auto|lexical|hybrid|multimodal]' >&2
      exit 2
    fi
    question="$1"
    mode="${2:-auto}"
    exec python3 tools/opencode_demo.py live --question "${question}" --mode "${mode}" --provider api
    ;;
  import)
    if [[ $# -lt 3 ]]; then
      echo 'usage: demo.sh import <wiki-root> <caption-package>' >&2
      exit 2
    fi
    exec python3 app.py ingest-wiki "$2" --caption-package "$3"
    ;;
  *)
    echo 'usage: demo.sh {start|status|tour|compare|questions|table|visual|refuse|preflight|serve|query|import}' >&2
    exit 2
    ;;
esac
