#!/usr/bin/env bash
# Abre o Niche Finder em http://127.0.0.1:8765
cd "$(dirname "$0")" && exec python3 server.py "$@"
