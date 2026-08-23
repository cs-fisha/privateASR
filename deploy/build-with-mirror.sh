#!/usr/bin/env bash
set -euo pipefail

# Use this only when registry-1.docker.io is unreachable. Change the mirror if needed.
MIRROR_BASE="${ASR_PYTHON_MIRROR:-docker.m.daocloud.io/library/python:3.11-slim}"
docker pull "$MIRROR_BASE"
docker tag "$MIRROR_BASE" python:3.11-slim
cd "$(dirname "$0")/.."
docker compose build
