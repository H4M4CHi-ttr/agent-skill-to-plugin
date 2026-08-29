from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agent_skill_to_plugin.compatibility import scan_claude_components


class RecursiveClaudeCompatibilityTests(unittest.TestCase):
    def test_nested_skill_components_are_reported_without_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "skills" / "example" / "agents").mkdir(parents=True)
            (root / "skills" / "example" / "agents" / "reviewer.md").write_text(
                "fixture",
                encoding="utf-8",
            )
            (root / "skills" / "example" / ".mcp.json").write_text(
                "{}",
                encoding="utf-8",
            )

            diagnostics = scan_claude_components(root)

        paths = {item.path for item in diagnostics if item.code == "nested_claude_component"}
        self.assertEqual(
            {"skills/example/agents", "skills/example/.mcp.json"},
            paths,
        )
        self.assertTrue(all(item.details.get("converted") is False for item in diagnostics))


if __name__ == "__main__":
    unittest.main()
