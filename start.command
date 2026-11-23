#!/bin/zsh
cd "$(dirname "$0")"
open http://localhost:8766 &
python3 monitor.py
