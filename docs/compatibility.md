# Compatibility

## Specification snapshot

This document records product-format facts checked on **2026-08-29** against the [official OpenAI plugin documentation](https://developers.openai.com/plugins/build/plugins), plus the standard personal-Marketplace convention checked on **2026-08-30** against the bundled `plugin-creator` workflow. Product behavior can change; verify the primary source and current local tooling before making release-critical assumptions.

At that snapshot:

- every Plugin has `.codex-plugin/plugin.json`;
- a Skill is stored under `skills/<name>/SKILL.md`;
- a minimal Plugin manifest includes `name`, `version`, `description`, and `skills`;
- a local/repository Marketplace manifest is `.agents/plugins/marketplace.json`;
- the standard personal convention stores Plugins at `~/plugins/<plugin-name>` and the Marketplace at `~/.agents/plugins/marketplace.json`, which is discovered implicitly;
- an explicit non-default repository Marketplace still requires `codex plugin marketplace add <path>`; and
- local/repository Marketplace availability varies by product surface.

The tool generates only skills-only Plugins. It does not add MCP servers, apps, hooks, or other capabilities merely because the source Claude Plugin contains them.

Local `run` and `convert` follow the standard personal convention by default. `--no-register-personal` explicitly limits the operation to workspace artifacts. Registration updates Marketplace availability only; it does not call `codex plugin add`, install or reinstall a Plugin, publish, or push. Results therefore report `installation_performed: false`; a forced divergent replacement reports `status: "updated"` and `reinstall_required: true` so callers can request an explicit reinstall separately. Divergent same-name personal state requires `--force-personal`, independently of workspace `--force`. After packaging succeeds but registration fails, `register-personal --plugin-dir <generated-plugin-dir>` retries only registration. Registration uses an ownership-checked lock and recovery journal; cleanup and rollback are best effort, so retained support state must be inspected rather than removed by guessing ownership. A Chat-only/cloud context is not assumed to have access to the user's local home directory, so personal registration requires a local Desktop/Codex or CLI execution context.

## Centralized policy

`scripts/agent_skill_to_plugin/limits.py` contains the executable format/resource policy and records:

- tool version;
- JSON schema version;
- specification snapshot date;
- Plugin/Skill identity and description limits;
- path, file-count, byte-size, archive, HTTP, and report bounds; and
- the default generated Plugin version.

Some values are intentionally conservative Agent Skill to Plugin policy. They must not be described as official OpenAI hard limits unless an official source explicitly says so. When OpenAI specifications change, update the centralized values, this dated document, tests, and `CHANGELOG.md` together.

## Runtime and operating systems

The preferred runtime is `uv`. `scripts/skill_to_plugin.py` declares Python 3.10+ and PyYAML 6.x with PEP 723 inline metadata, allowing `uv run` to create an isolated environment and obtain a compatible Python when needed. `npx skills add` does not install `uv`, and the Skill never installs `uv` automatically.

The fallback is a separately installed Python 3.10+ with PyYAML 6.x. PyYAML remains declared in `pyproject.toml` and `requirements.txt`; it is resolved separately rather than vendored. The repository includes a GitHub Actions workflow definition for Windows, Linux, and macOS. A workflow file is not evidence that a particular commit passed hosted CI; consult the repository's actual Actions results.

Filesystem validation intentionally applies portable constraints even when running on a single platform. Windows reserved names, trailing-dot/space rules, case-insensitive collisions, POSIX ZIP separators, Unicode normalization, symlinks, and reparse points are considered during packaging.

## Source-specific dependencies

| Source | Required dependency | Authentication behavior |
|---|---|---|
| local file/directory/archive | no additional source tool beyond the selected runtime | none |
| HTTPS Skill/archive/npm tarball | network access from the selected runtime | no credentials in URLs; public npm registry in 0.6.0 |
| GitHub/Git | Git executable | existing Git/SSH configuration only |
| npx skills | Node.js/npm/npx | isolated npm cache/config; acquisition itself performs no user-home Skill installation |
| Claude Marketplace discovery | optional Claude executable | only `plugin marketplace list --json`, read-only |

If a source-specific dependency is missing, unrelated local functionality remains usable.

## Agent Skill front matter

Front matter is parsed as YAML with safe loading and explicit validation. The tool requires the fields needed to construct a valid Skill and preserves supported additional source metadata in the copied `SKILL.md`. Malformed YAML and type errors are reported as candidate diagnostics.

OpenAI and source ecosystems may accept different optional front-matter keys. Passing static validation does not guarantee that every optional source-specific instruction works unchanged in ChatGPT/Codex. On the date above, the OpenAI Plugin validator bundled with the tested local Codex environment rejected a generated Plugin retaining `disable-model-invocation: true` or a `...` front-matter closer; current public documentation does not describe the invocation field. The tool therefore normalizes those forms only in the generated copy and expresses explicit-only intent in `agents/openai.yaml` with `policy.allow_implicit_invocation: false`. This policy must be verified on each target surface/version. Existing agent metadata is filtered through the 0.6.0 conservative allowlist, default prompts must mention the Skill token, icon paths are validated, and exact field paths plus source/generated hashes are reported under `compatibility_adaptations`.

## Claude compatibility diagnostics

Static diagnostics cover Claude/Anthropic product references, `${CLAUDE_PLUGIN_ROOT}`, `${CLAUDE_PROJECT_DIR}`, slash commands, `commands/`, `agents/`, hooks, `.mcp.json`/`mcpServers`, settings/userConfig, live artifacts, LSP, monitors, and Plugin dependencies where detectable.

Those items are not automatically rewritten. If a Claude Plugin has no valid `SKILL.md`, it cannot be extracted as a skills-only Plugin; commands/agents require a separate explicit semantic conversion process.

## JSON and exit-code compatibility

JSON output uses `schema_version: "1.0"` in 0.6.0. Process exit categories are:

| Code | Meaning |
|---:|---|
| 0 | success/resolved |
| 10 | Skill selection required |
| 11 | additional source/Marketplace input required |
| 20 | unknown input format |
| 21 | unknown source |
| 22 | unknown Marketplace |
| 23 | unknown Plugin |
| 24 | no Skill candidates |
| 25 | authentication failed |
| 26 | network failed |
| 27 | invalid manifest |
| 28 | unsupported source |
| 29 | security rejection |
| 30 | package validation failed |
| 31 | dependency missing |
| 32 | invalid selection |
| 33 | resolution integrity failed |
| 34 | output conflict |
| 35 | personal Marketplace registration failed |
| 70 | unexpected internal error |

Callers should inspect both `status` and `error_code`, not parse human-readable messages.

`run` and `convert` retain `zip_path` and `zip_sha256` in versioned JSON output and the JSON conversion report. Ordinary human output shows those values only with `--show-zip`; the Markdown conversion report does not surface the ZIP.

## Legacy wrapper

`scripts/pluginize.py` accepts the old `--command-file`, `--output-root`, and related prototype arguments and delegates to the new pipeline. The wrapper exists for migration; it is not the preferred public API and can expose less precise legacy exit behavior. Migrate automation to `agent-skill-to-plugin run|resolve|convert` and versioned JSON.

The prototype's unpublished `1.0.0` label is not a compatibility claim for the new architecture. The first public beta is 0.5.0, as explained in [CHANGELOG.md](../CHANGELOG.md).
