#!/bin/bash

cd "$(dirname "$0")"

python3 -m pip install --upgrade pip

pip3 install -r requirements.txt

python3 pipeline.py

