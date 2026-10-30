#!/usr/bin/env python3
"""Scheduled entry. Hermes runs this with the repo's virtualenv Python."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

os.environ.setdefault("ENV_FILE", "/etc/expense-agent-v2/env")
agent = Path(__file__).resolve().parent.parent / "agent.py"
sys.argv = [str(agent), "poll"]
runpy.run_path(str(agent), run_name="__main__")
