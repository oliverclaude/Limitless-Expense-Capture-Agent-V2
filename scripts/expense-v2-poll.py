#!/usr/bin/env python3
"""Scheduled entry. Hermes runs this with the repo's virtualenv Python."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

os.environ.setdefault("ENV_FILE", "/etc/expense-agent-v2/env")
home = ""
for line in Path(os.environ["ENV_FILE"]).read_text(encoding="utf-8").splitlines():
    if line.startswith("AGENT_HOME="):
        home = line.split("=", 1)[1].strip().strip("'\"")
        break
if not home:
    raise SystemExit("AGENT_HOME is empty")
agent = Path(home) / "agent.py"
repo = str(agent.parent)
if repo not in sys.path:
    sys.path.insert(0, repo)
sys.argv = [str(agent), "poll"]
runpy.run_path(str(agent), run_name="__main__")
