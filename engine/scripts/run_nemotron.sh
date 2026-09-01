#!/usr/bin/env bash
# Run the briefing tool on NVIDIA NIM (Nemotron).
#
# Setup once:
#   cp .env.example .env        # then paste your nvapi- key into .env
#   chmod +x run_nemotron.sh
#
# Usage:
#   ./run_nemotron.sh                                   # runs the sample brief
#   ./run_nemotron.sh /path/to/client_brief.pdf "Northwind Moving People"
#
# Model override:  BRIEF_MODEL=nvidia/llama-3.3-nemotron-super-49b-v1 ./run_nemotron.sh
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root (script lives in scripts/)

# load .env if present (KEY=VALUE lines), without printing it
[ -f .env ] && set -a && . ./.env && set +a

: "${NVIDIA_API_KEY:?Set NVIDIA_API_KEY (in .env or the environment) first}"

python3 parse_brief.py "${1:-samples/messy_brief_sample.txt}" \
    --provider nim \
    --model "${BRIEF_MODEL:-nvidia/llama-3.1-nemotron-70b-instruct}" \
    --project "${2:-Moving People}"
