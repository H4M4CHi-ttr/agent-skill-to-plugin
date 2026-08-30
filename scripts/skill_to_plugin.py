#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "PyYAML>=6.0.2,<7",
# ]
# ///
"""Source-tree entry point for Agent Skill to Plugin."""

from agent_skill_to_plugin.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
