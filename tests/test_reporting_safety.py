from __future__ import annotations

import unittest

from agent_skill_to_plugin.reporting import render_markdown


class MarkdownReportingSafetyTests(unittest.TestCase):
    def test_untrusted_fields_cannot_create_markdown_structure(self) -> None:
        report = {
            "plugin_name": "safe-plugin",
            "created_at": "2026-08-29T00:00:00Z",
            "plugin_tree_sha256": "a" * 64,
            "zip_sha256": "b" * 64,
            "plugin_dir": "path`with`ticks",
            "zip_path": "package.zip",
            "marketplace_root": "root",
            "marketplace_file": "marketplace.json",
            "marketplace_add_command": "codex plugin marketplace add root",
            "provenance": {
                "tool_version": "0.5.0",
                "input_kind": "local",
                "normalized_source": "source",
                "source_snapshot_sha256": "c" * 64,
            },
            "skills": [{
                "name": "safe-skill",
                "path": "skills/safe",
                "candidate_id": "candidate",
                "tree_sha256": "d" * 64,
                "description": "# forged\n[link](https://attacker.invalid)",
            }],
            "diagnostics": [{
                "severity": "warning",
                "code": "fixture",
                "message": "# forged diagnostic",
            }],
        }

        rendered = render_markdown(report)
        self.assertNotIn("\n# forged\n", rendered)
        self.assertIn(r"\# forged \[link\]\(https://attacker\.invalid\)", rendered)
        self.assertIn("`` path`with`ticks ``", rendered)


if __name__ == "__main__":
    unittest.main()
