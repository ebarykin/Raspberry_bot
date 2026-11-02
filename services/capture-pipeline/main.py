#!/usr/bin/env python3
"""Тонкая обёртка: python main.py --config configs/..."""

from __future__ import annotations

import sys
from pathlib import Path

# Позволяет запускать из корня репозитория без PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from capture_pipeline.app import main

if __name__ == "__main__":
    raise SystemExit(main())
