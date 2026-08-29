from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from agent_skill_to_plugin.fetchers.npx import NpxFetcher, _resolve_npx_prefix
from agent_skill_to_plugin.input_parser import parse_npx_command


SKILL_TEXT = "---\nname: example\ndescription: Local npx fixture.\n---\nBody.\n"


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        copied_kwargs = dict(kwargs)
        self.calls.append((list(argv), copied_kwargs))
        destination = Path(copied_kwargs["cwd"])
        skill = destination / ".agents" / "skills" / "example"
        (skill / "scripts").mkdir(parents=True)
        (skill / "SKILL.md").write_text(SKILL_TEXT, encoding="utf-8")
        (skill / "scripts" / "installer.py").write_text(
            "raise AssertionError('imported scripts must not run')\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, stdout="installed", stderr="")


class NpxFetcherIsolationTests(unittest.TestCase):
    def test_fixed_argv_shell_false_and_project_isolation(self) -> None:
        parsed = parse_npx_command(
            "npx.cmd --yes skills@latest add owner/repo --global "
            "--agent claude --skill example --full-depth"
        )
        runner = RecordingRunner()
        prefix = [r"C:\runtime\node.exe", r"C:\runtime\npx-cli.js"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "isolated-project"
            sentinel = root / "imported-script-ran"
            result = NpxFetcher(npx_prefix=prefix, runner=runner).fetch(
                parsed,
                destination,
                extra_env={
                    "npm_config_ignore_scripts": "false",
                    "AGENT_SKILL_TO_PLUGIN_TEST_SENTINEL": str(sentinel),
                },
            )

            self.assertEqual(1, len(runner.calls))
            argv, kwargs = runner.calls[0]
            self.assertEqual(
                prefix
                + [
                    "--yes",
                    "skills@latest",
                    "add",
                    "owner/repo",
                    "--skill=example",
                    "--full-depth",
                    "--agent",
                    "codex",
                    "--copy",
                    "--yes",
                ],
                argv,
            )
            self.assertIs(False, kwargs["shell"])
            self.assertEqual(destination.resolve(), kwargs["cwd"])
            self.assertEqual(subprocess.DEVNULL, kwargs["stdin"])
            self.assertIs(False, kwargs["check"])
            self.assertFalse(sentinel.exists())
            self.assertEqual(
                (destination.resolve() / ".agents" / "skills" / "example",),
                result.installed_skill_dirs,
            )

            env = kwargs["env"]
            self.assertIsInstance(env, dict)
            assert isinstance(env, dict)
            self.assertEqual("true", env["npm_config_ignore_scripts"])
            self.assertEqual("false", env["npm_config_package_lock"])
            self.assertEqual("false", env["npm_config_audit"])
            self.assertEqual("false", env["GIT_CONFIG_VALUE_0"])
            self.assertEqual("0", env["GIT_TERMINAL_PROMPT"])
            self.assertIn("agent-skill-to-plugin-npm-", env["npm_config_cache"])
            self.assertIn("agent-skill-to-plugin-npm-", env["npm_config_prefix"])
            self.assertIn("agent-skill-to-plugin-npm-", env["npm_config_userconfig"])
            self.assertNotIn("--global", argv)
            self.assertNotIn("claude", argv)

    def test_select_all_is_one_non_smuggleable_argv_element(self) -> None:
        parsed = parse_npx_command("npx skills add owner/repo --all")
        runner = RecordingRunner()
        with tempfile.TemporaryDirectory() as temporary:
            NpxFetcher(npx_prefix=["npx-safe"], runner=runner).fetch(
                parsed,
                Path(temporary) / "project",
            )
        argv, _ = runner.calls[0]
        self.assertIn("--skill=*", argv)
        self.assertNotIn("*", argv)

    @unittest.skipUnless(os.name == "nt", "npx.cmd launcher layout is Windows-specific")
    def test_windows_npx_cmd_resolves_to_node_and_javascript_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            npx_cmd = root / "npx.cmd"
            node = root / "node.exe"
            cli = root / "node_modules" / "npm" / "bin" / "npx-cli.js"
            cli.parent.mkdir(parents=True)
            for path in (npx_cmd, node, cli):
                path.write_text("fixture", encoding="utf-8")
            values = {"npx.cmd": str(npx_cmd), "node.exe": str(node)}
            prefix = _resolve_npx_prefix(values.get)
            self.assertEqual([str(node.resolve()), str(cli.resolve())], prefix)


if __name__ == "__main__":
    unittest.main()
