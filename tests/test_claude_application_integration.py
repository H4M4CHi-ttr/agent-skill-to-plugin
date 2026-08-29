from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agent_skill_to_plugin.application import run_request
from agent_skill_to_plugin.models import ConversionResult

from tests.helpers import FIXTURES


class ClaudeApplicationIntegrationTests(unittest.TestCase):
    def test_inline_local_marketplace_flows_through_registry_and_packages_all_skills(self) -> None:
        marketplace = (FIXTURES / "claude-marketplace-relative").resolve().as_posix()
        request = (
            f"/plugin marketplace add {marketplace}\n"
            "/plugin install demo-relative@fixture-marketplace"
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "converted"
            result = run_request(
                request,
                output_root=output,
                source_base=Path(temporary),
                timeout_seconds=7,
            )

            self.assertIsInstance(result, ConversionResult)
            assert isinstance(result, ConversionResult)
            self.assertEqual({"alpha", "beta"}, {item.name for item in result.skills})
            self.assertTrue(Path(result.zip_path).is_file())
            self.assertTrue(any("commands" in warning.casefold() for warning in result.warnings))


if __name__ == "__main__":
    unittest.main()
