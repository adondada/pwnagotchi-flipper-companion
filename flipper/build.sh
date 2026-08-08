#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
ufbt
printf '\nBuilt FAP(s):\n'
find dist -maxdepth 1 -type f -name '*.fap' -print
