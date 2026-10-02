"""Scripted-driver scripts and narratives as *data*, for the untrusted
agent process's deterministic self-check. Loaded from
`blackwall/ipc/scripted_tasks.json` (generated from tasks.py; a test keeps
them in sync) so the agent process imports no trusted module. Contains no
prompts (the trusted side issues them) and no policy."""

import json
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ipc", "scripted_tasks.json")


def task_scripts() -> dict:
    with open(_PATH, encoding="utf-8") as fh:
        return json.load(fh)
