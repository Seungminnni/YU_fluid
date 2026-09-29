#!/bin/zsh
set -e
cd "$(dirname "$0")"
[[ -f pyproject.toml ]] || cd ..
[[ -f pyproject.toml ]] || { echo 'Extract the whole project first.'; exit 1; }
for candidate in .venv/bin/python python3 python3.14 python3.13 python3.12; do
  if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' >/dev/null 2>&1; then
    exec "$candidate" tools/launch_workflow.py "$@"
  fi
done
echo 'Python 3.12 or higher is required. Python 3.13 and 3.14 are allowed.'
exit 1
