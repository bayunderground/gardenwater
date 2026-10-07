#!/usr/bin/env python3
"""Thin entry point for cron: run the garden-water check once and exit."""

import sys

from gardenwater.app import main

if __name__ == "__main__":
    sys.exit(main())
