#!/usr/bin/env bash
set -euo pipefail

python3 -m src.semantic_registry.cli validate "$@"
