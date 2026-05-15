#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
python3 pipeline.py --blocks blocks.txt --project "15 пытки НКВД"
