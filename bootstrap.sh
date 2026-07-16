#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Create or update the local OmicsANG environment, then launch the installed CLI.
set -Eeuo pipefail
IFS=$'\n\t'

if [[ "$(uname -s)" != "Linux" ]]; then
  printf '%s\n' 'error: OmicsANG supports Linux only; use WSL on Windows.' >&2
  exit 1
fi

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/environment.yml"
ENV_NAME="omicsang"

if [[ ! -f "${ENV_FILE}" ]]; then
  printf 'error: environment file not found: %s\n' "${ENV_FILE}" >&2
  exit 1
fi

CONDA_EXE=""
for candidate in micromamba mamba conda; do
  if command -v "${candidate}" >/dev/null 2>&1; then
    CONDA_EXE="$(command -v "${candidate}")"
    break
  fi
done

if [[ -z "${CONDA_EXE}" ]]; then
  printf 'error: need micromamba, mamba, or conda on PATH.\n' >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  printf 'error: need python3 on PATH to validate environment metadata.\n' >&2
  exit 1
fi

env_exists="$({ "${CONDA_EXE}" env list --json; } | python3 -c '
import json
import sys
from pathlib import Path

name = sys.argv[1]
payload = json.load(sys.stdin)
print("yes" if any(Path(path).name == name for path in payload.get("envs", [])) else "no")
' "${ENV_NAME}")"

if [[ "${env_exists}" == "yes" ]]; then
  printf 'Updating the %s environment...\n' "${ENV_NAME}"
  (
    cd -- "${SCRIPT_DIR}"
    "${CONDA_EXE}" env update --name "${ENV_NAME}" --file "${ENV_FILE}" --prune
  )
else
  printf 'Creating the %s environment...\n' "${ENV_NAME}"
  (
    cd -- "${SCRIPT_DIR}"
    "${CONDA_EXE}" env create --name "${ENV_NAME}" --file "${ENV_FILE}"
  )
fi

printf 'Launching OmicsANG...\n'
case "$(basename -- "${CONDA_EXE}")" in
  conda)
    exec "${CONDA_EXE}" run --no-capture-output --name "${ENV_NAME}" omicsang "$@"
    ;;
  *)
    exec "${CONDA_EXE}" run --name "${ENV_NAME}" omicsang "$@"
    ;;
esac
