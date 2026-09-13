#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
cd "$project_root"

python_command="${DOCSENSE_BUILD_PYTHON:-python3.11}"
command -v "$python_command" >/dev/null
command -v cc >/dev/null
"$python_command" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)'

architecture="$(uname -m)"
build_venv="$project_root/.runtime/native-build-venv-linux-$architecture"
venv_python="$build_venv/bin/python"

if [[ ! -x "$venv_python" ]]; then
    "$python_command" -m venv "$build_venv"
fi

"$venv_python" -m pip install --disable-pip-version-check "Cython==3.1.1"
"$venv_python" "$project_root/scripts/build_license_native.py" \
    --output-dir "$project_root/.runtime/native-license/linux-$architecture-cpython311" \
    --build-dir "$project_root/.runtime/native-license-build/linux-$architecture-cpython311"
