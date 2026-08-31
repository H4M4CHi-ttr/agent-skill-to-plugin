from __future__ import annotations

import unittest

from agent_skill_to_plugin.errors import NeedsInputError, SkillToPluginError
from agent_skill_to_plugin.input_parser import parse_input, parse_npx_command


class NpxInputParserTests(unittest.TestCase):
    def test_normal_npx_and_isolation_only_options(self) -> None:
        parsed = parse_input(
            "npx skills add vercel-labs/agent-skills "
            "--skill web-design-guidelines --global --agent claude"
        )
        self.assertEqual("npx_skills", parsed.kind)
        self.assertEqual("vercel-labs/agent-skills", parsed.source)
        self.assertEqual(("web-design-guidelines",), parsed.requested_skills)
        self.assertEqual("npx", parsed.metadata["npx_executable"])
        self.assertNotIn("agent", parsed.metadata)

    def test_npx_cmd_and_versioned_canonical_package(self) -> None:
        parsed = parse_input(
            "npx.cmd --yes skills@latest add owner/repository --skill=example"
        )
        self.assertEqual("npx_skills", parsed.kind)
        self.assertEqual("npx.cmd", parsed.metadata["npx_executable"])
        self.assertEqual("skills@latest", parsed.metadata["package_spec"])
        self.assertEqual(("example",), parsed.requested_skills)

    def test_powershell_bash_and_cmd_continuations(self) -> None:
        cases = (
            "npx.cmd --yes `\n skills@latest add owner/repo `\n --skill demo",
            "npx skills add owner/repo \\\n --skill demo",
            "npx skills add owner/repo ^\n --skill demo",
        )
        for value in cases:
            with self.subTest(value=value):
                parsed = parse_input(value)
                self.assertEqual(("demo",), parsed.requested_skills)

    def test_markdown_code_fence_and_inline_code(self) -> None:
        fenced = parse_input(
            "Please port this:\n```powershell\n"
            "npx.cmd --yes skills@latest add owner/repo --skill demo\n```"
        )
        inline = parse_input("Please use `npx skills add owner/repo --skill demo`.")
        self.assertEqual("npx_skills", fenced.kind)
        self.assertEqual("npx_skills", inline.kind)
        self.assertEqual(("demo",), fenced.requested_skills)
        self.assertEqual(("demo",), inline.requested_skills)

    def test_github_url_can_be_markdown_or_prose(self) -> None:
        target = "https://github.com/mattpocock/skills"
        markdown = parse_input(f"[{target}]({target})")
        prose = parse_input(f"Convert this repository for ChatGPT:\n{target}")
        self.assertEqual("github_repository", markdown.kind)
        self.assertEqual(target, markdown.normalized_input)
        self.assertEqual(markdown.normalized_input, prose.normalized_input)

    def test_github_query_and_fragment_are_not_part_of_the_source(self) -> None:
        parsed = parse_input(
            "https://github.com/Owner/Repo/tree/main/skills/example?tab=readme#usage"
        )
        self.assertEqual("github_path", parsed.kind)
        self.assertEqual(
            "https://github.com/Owner/Repo/tree/main/skills/example",
            parsed.normalized_input,
        )

    def test_windows_relative_local_path_is_recognized(self) -> None:
        parsed = parse_input(r"work\fixtures\skill")
        self.assertEqual("local", parsed.kind)
        self.assertEqual(r"work\fixtures\skill", parsed.source)

    def test_scp_like_git_source_is_recognized(self) -> None:
        parsed = parse_input("git@github.com:owner/repository.git")
        self.assertEqual("git_url", parsed.kind)
        self.assertEqual("git@github.com:owner/repository.git", parsed.source)

    def test_npx_github_tree_source_remains_one_logical_request(self) -> None:
        url = "https://github.com/vercel-labs/agent-skills/tree/main/skills/example"
        parsed = parse_input(f"npx skills add {url} --skill example")
        self.assertEqual("npx_skills", parsed.kind)
        self.assertEqual(url, parsed.source)
        self.assertEqual(1, len(parsed.logical_sources))

    def test_chat_markdown_transport_is_one_npx_request(self) -> None:
        raw = (
            r"[$agent-skill-to-plugin](C:\Users\dhama\.agents\skills\agent-skill-to-plugin\SKILL.md) "
            "npx skills add "
            "[https://github.com/mattpocock/skills](https://github.com/mattpocock/skills) "
            "--skill grilling"
        )

        parsed = parse_input(raw)

        self.assertEqual("npx_skills", parsed.kind)
        self.assertEqual("https://github.com/mattpocock/skills", parsed.source)
        self.assertEqual(("grilling",), parsed.requested_skills)
        self.assertEqual(("https://github.com/mattpocock/skills",), parsed.logical_sources)
        self.assertEqual(raw, parsed.raw_input)

    def test_command_autolink_does_not_absorb_an_unrelated_url(self) -> None:
        with self.assertRaises(NeedsInputError) as raised:
            parse_input(
                "npx skills add "
                "[https://github.com/example/one](https://github.com/example/one) "
                "--skill demo\n"
                "https://github.com/example/two"
            )

        choices = raised.exception.details["choices"]
        self.assertEqual(["npx_skills", "github_repository"], [item["kind"] for item in choices[:-1]])


class ClaudeInputParserTests(unittest.TestCase):
    def test_slash_and_cli_install_forms(self) -> None:
        slash = parse_input("/plugin install skill-creator@claude-plugins-official")
        cli = parse_input("claude plugin install skill-creator@claude-plugins-official")
        for parsed in (slash, cli):
            self.assertEqual("claude_plugin", parsed.kind)
            self.assertEqual("skill-creator", parsed.plugin_name)
            self.assertEqual("claude-plugins-official", parsed.marketplace_name)
            self.assertTrue(parsed.plugin_scope)

    def test_marketplace_add_and_install_are_one_logical_request(self) -> None:
        parsed = parse_input(
            "/plugin marketplace add mattpocock/skills\n"
            "/plugin install mattpocock-skills@mattpocock"
        )
        self.assertEqual("claude_plugin", parsed.kind)
        self.assertEqual("mattpocock/skills", parsed.marketplace_source)
        self.assertEqual("mattpocock-skills", parsed.plugin_name)
        self.assertEqual("mattpocock", parsed.marketplace_name)
        self.assertTrue(parsed.plugin_scope)


class LogicalRequestTests(unittest.TestCase):
    def test_unrelated_sources_require_structured_choice(self) -> None:
        with self.assertRaises(NeedsInputError) as raised:
            parse_input(
                "https://github.com/example/one\n"
                "https://github.com/example/two"
            )
        self.assertEqual("multiple_sources", raised.exception.details["prompt_kind"])
        choices = raised.exception.details["choices"]
        self.assertEqual("combine-all", choices[-1]["id"])
        self.assertEqual(3, len(choices))

    def test_explicit_combine_produces_structured_multi_source(self) -> None:
        parsed = parse_input(
            "Combine all sources into one plugin:\n"
            "https://github.com/example/one\n"
            "https://github.com/example/two"
        )
        self.assertEqual("multi_source", parsed.kind)
        self.assertTrue(parsed.select_all)
        self.assertEqual("explicit-all", parsed.metadata["combination"])
        self.assertEqual(2, len(parsed.metadata["sources"]))

    def test_unknown_prose_has_stable_error_code(self) -> None:
        with self.assertRaises(SkillToPluginError) as raised:
            parse_input("Please make something useful from this.")
        self.assertEqual("unknown_input_format", raised.exception.code)


class InputSecurityTests(unittest.TestCase):
    def test_shell_operators_are_never_accepted(self) -> None:
        hazardous = (
            "npx skills add owner/repo && whoami",
            "npx skills add owner/repo || whoami",
            "npx skills add owner/repo; whoami",
            "npx skills add owner/repo | whoami",
            "npx skills add owner/repo > output.txt",
            "npx skills add owner/repo < input.txt",
            "npx skills add $(whoami)",
            "npx skills add ${SOURCE}",
            "npx skills add owner/repo & whoami",
        )
        for command in hazardous:
            with self.subTest(command=command):
                with self.assertRaises(SkillToPluginError) as raised:
                    parse_npx_command(command)
                self.assertEqual("security_rejected", raised.exception.code)

    def test_embedded_url_credentials_and_secret_query_are_rejected(self) -> None:
        values = (
            "https://user:password@github.com/example/repo",
            "https://github.com/example/repo?access_token=not-a-real-token",
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(SkillToPluginError) as raised:
                    parse_input(value)
                self.assertEqual("security_rejected", raised.exception.code)

    def test_option_smuggling_unknown_options_and_package_aliases_are_rejected(self) -> None:
        commands = (
            "npx skills add owner/repo --skill=--agent",
            "npx skills add owner/repo --agent=--skill",
            "npx skills add owner/repo --unknown value",
            "npx skills add owner/repo -- --agent attacker",
            "npx evil-skills add owner/repo",
            "npx @attacker/skills add owner/repo",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(SkillToPluginError) as raised:
                    parse_npx_command(command)
                self.assertEqual("security_rejected", raised.exception.code)

    def test_command_markdown_link_cannot_hide_a_different_target(self) -> None:
        with self.assertRaises(SkillToPluginError) as raised:
            parse_input(
                "npx skills add "
                "[https://github.com/example/visible](https://github.com/example/target) "
                "--skill demo"
            )

        self.assertEqual("security_rejected", raised.exception.code)

    def test_command_markdown_autolink_still_rejects_credentials(self) -> None:
        source = "https://user:password@github.com/example/repo"
        with self.assertRaises(SkillToPluginError) as raised:
            parse_input(f"npx skills add [{source}]({source}) --skill demo")

        self.assertEqual("security_rejected", raised.exception.code)

    def test_command_markdown_title_cannot_erase_shell_syntax(self) -> None:
        source = "https://github.com/example/repo"
        with self.assertRaises(SkillToPluginError) as raised:
            parse_input(
                f'npx skills add [{source}]({source} "&& whoami") --skill demo'
            )

        self.assertEqual("security_rejected", raised.exception.code)

    def test_transport_skill_link_cannot_hide_shell_syntax(self) -> None:
        with self.assertRaises(SkillToPluginError) as raised:
            parse_input(
                r"[$agent-skill-to-plugin](C:\tmp;whoami\skills\agent-skill-to-plugin\SKILL.md) "
                "npx skills add owner/repo --skill demo"
            )

        self.assertEqual("security_rejected", raised.exception.code)

    def test_nonmatching_local_markdown_link_is_not_transport_metadata(self) -> None:
        with self.assertRaises(NeedsInputError):
            parse_input(
                r"[$agent-skill-to-plugin](C:\tmp\skills\different-skill\SKILL.md) "
                "npx skills add owner/repo --skill demo"
            )


if __name__ == "__main__":
    unittest.main()
