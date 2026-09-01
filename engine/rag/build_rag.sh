#!/usr/bin/env bash
# Build the planner/effectiveness RAG index from reference/rag/, using NIM embeddings.
#   ./build_rag.sh                 # builds ../reference/rag → ./index
#   ./build_rag.sh /path/to/corpus
# Needs NVIDIA_API_KEY (real NIM embeddings). Dry-run offline: RAG_EMBED=offline ./build_rag.sh
set -euo pipefail
cd "$(dirname "$0")"
[ -f ../.env ] && set -a && . ../.env && set +a
: "${NVIDIA_API_KEY:?Set NVIDIA_API_KEY in briefing/.env (or RAG_EMBED=offline to dry-run)}"
python3 rag.py build --corpus "${1:-../reference/rag}" --index ./index
echo "Try:  python3 rag.py query --index ./index \"challenger brand, nervous CMO\" -k 5"
