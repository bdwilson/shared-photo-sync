#!/bin/bash

# 1. Ensure Virtual Environment exists
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

# 2. Run in Dry Run mode by default
echo "🚀 Running Apple Shared Album Sync 5 albums (dry-run)"
python3 sync_albums.py --num 5 --dry-run --verbose
