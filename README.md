# Agent Skill to Plugin

[日本語](README.ja.md)

Agent Skill to Plugin safely packages existing Agent Skills as OpenAI skills-only plugins for ChatGPT and Codex, then registers validated results in the standard personal Marketplace. It resolves sources from several ecosystems and keeps immutable snapshots, deterministic selection, validation, provenance, packaging, registration, and reporting as separate steps. Remote content is always treated as untrusted data.

Version 0.6.0 is a public beta. Review generated warnings and the upstream license before installing or redistributing any result.

This is an independent open-source project. It is not affiliated with or endorsed by OpenAI or Anthropic.

## Why turn a Skill into a plugin?

One major goal of this project is to make reusable Agent Skills available in ordinary ChatGPT Chat, not only in Work or Codex. [OpenAI's plugin model](https://developers.openai.com/plugins/build/plugins) gives a Skill an installable identity and tells ChatGPT and Codex which workflows and packaged resources belong together. After conversion and installation, an instruction- or resource-driven Skill can be used from a regular Chat conversation instead of remaining limited to agent-oriented work surfaces.

Chat has an important boundary: it is primarily a conversational surface, not a general-purpose local-filesystem environment. Do not design the Chat path around reading from or writing to arbitrary paths or repository trees on the user's computer. In Chat, favor conversational workflows, packaged resources, files the user explicitly supplies, and tools that the current surface exposes. Use Work for file-centered deliverables, and use Codex or the CLI for repository and local-filesystem workflows. This distinction follows [OpenAI's current product guidance](https://learn.chatgpt.com/); exact availability still depends on the product surface, rollout, and workspace policy.

As of 2026-08-29, [OpenAI's documented installation and testing path](https://developers.openai.com/plugins/build/plugins) for local and repository Marketplace plugins uses the ChatGPT desktop app. If this feature is available on your individual account, including Pro, treat the desktop app as the currently documented path for this tool's local-Marketplace output. OpenAI's documentation does not currently provide a per-individual-plan support matrix, so this does not establish that Pro alone is restricted or that every individual plan has identical access. Public plugins and workspace-published plugins use different distribution paths.

## What it solves

Agent Skills are published as repository directories, `npx skills add` commands, Claude Plugin entries, archives, and local folders. Those inputs do not all have the same boundaries or manifests. Agent Skill to Plugin normalizes them into one model, discovers valid `SKILL.md` files, pauses when a structural choice is genuinely ambiguous, and emits:

- an OpenAI plugin directory containing only skills;
- a standard personal Marketplace registration for local runs;
- JSON and Markdown conversion reports; and
- a saved resolution that pins the source snapshot for a later selection turn.

The converter also creates a deterministic ZIP for provenance and explicit distribution requests. Ordinary human-readable output shows it only with `--show-zip`. Versioned JSON output and the JSON conversion report retain `zip_path` and `zip_sha256`; the Markdown report and Skill response do not surface the archive unless it was explicitly requested.

It does not translate Claude commands, agents, hooks, MCP servers, settings, or other non-Skill components into new behavior.

## Requirements

- Recommended runtime: [`uv`](https://docs.astral.sh/uv/getting-started/installation/). The Skill uses it internally to provision a compatible Python and PyYAML in an isolated environment.
- Fallback runtime: Python 3.10 or newer with PyYAML 6.x
- Git for GitHub and Git sources
- Node.js/npm/npx for the installation command below, and when importing an `npx skills add` request
- Claude CLI only as an optional, read-only aid for resolving already registered Claude Marketplaces

Install the Skill from the public repository:

```bash
npx skills add H4M4CHi-ttr/agent-skill-to-plugin
```

This `npx skills add` command is the sole supported end-user distribution path for Agent Skill to Plugin. The project does not publish a separate ZIP for installing this Skill. Archives produced by the converter are generated Plugin outputs, not installers for Agent Skill to Plugin itself, and are surfaced only when explicitly requested.

`npx skills add` installs the Skill files but does not install `uv`. Once `uv` is available on a supported execution surface, the Skill invokes `uv run` itself. A person using the Skill from ChatGPT or Codex normally does not need to choose a Python version, install PyYAML, or enter `uv` commands.

For development, or to prepare the Python fallback manually:

```bash
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

PyYAML 6.x is the only Python runtime dependency. It is declared both in the
Python package metadata and in the entry script's PEP 723 inline metadata. It
is not vendored: `uv` resolves it into an isolated environment on the preferred
path, while the Python package installer obtains it separately on the fallback
path.

On Windows PowerShell, replace `.venv/bin/python` above with `.venv\Scripts\python.exe` (or activate with `.venv\Scripts\Activate.ps1`). `npx.cmd` input is accepted, but the tool never sends it through a command shell.

## Quick start

### From a normal ChatGPT/Codex chat

After installing the Agent Skill to Plugin Skill/Plugin in a supported surface:

The converter itself needs access to the referenced source, an execution environment that can run `uv` (recommended) or the Python fallback, and local filesystem access to register the result. An ordinary Chat-only or cloud execution context cannot be assumed to resolve a path on your computer or write `~/plugins` and `~/.agents/plugins/marketplace.json`. Use a reachable GitHub/archive source and run the conversion in a local Desktop/Codex or CLI context; then install and use the registered Plugin in Chat. A surface may also let you provide files explicitly, but that does not by itself grant access to your local personal Marketplace.

1. Paste one install command, GitHub URL, or accessible local path into a local Desktop/Codex chat and ask to package it as an OpenAI plugin.
2. The Skill writes that logical request to a UTF-8 input file and runs the tool with `--json`.
3. If the response is `needs_selection`, choose by number, Skill name, path, or “all.” The Skill resumes from the saved resolution rather than fetching the branch again.
4. The tool places the validated Plugin under `~/plugins/<plugin-name>` and adds it to `~/.agents/plugins/marketplace.json`. Grant filesystem permission if the local execution environment requests it.
5. Review the returned Skill list, registration status, warnings, provenance, and JSON/Markdown reports, then install the resulting Plugin from the personal Marketplace in a supported surface.
6. Open a new chat and explicitly invoke the imported Skill for its first functional test.

This Skill is configured for explicit use because acquisition can access the network and execute local acquisition tools. It should not start implicitly from unrelated conversation.

### CLI

Put one logical import request in a UTF-8 file:

```text
npx skills add vercel-labs/agent-skills --skill web-design-guidelines
```

For manual CLI use, run with the recommended runtime:

```bash
uv run scripts/skill_to_plugin.py run \
  --input-file input.txt \
  --output-root converted-skills-marketplace \
  --json
```

If `uv` is unavailable and Python 3.10+ with PyYAML 6.x is already installed, replace `uv run` with `python` (or `python3`).

If exactly one Skill is selected by deterministic rules, `run` resolves and packages it. If several repository Skills remain plausible, it returns `status: needs_selection`, exit code 10, and a persistent resolution file. Resume without re-fetching the branch:

```bash
uv run scripts/skill_to_plugin.py convert \
  --resolution converted-skills-marketplace/resolutions/<resolution-id>.json \
  --select <candidate-id> \
  --json
```

`--select` also accepts an exact Skill name, exact repository path, or `all`. Repeat the option to select several explicit candidates.

For a resolve-only first phase:

```bash
uv run scripts/skill_to_plugin.py resolve \
  --input-file input.txt \
  --output-root converted-skills-marketplace \
  --json
```

The installed console entry point is equivalent:

```bash
agent-skill-to-plugin run --input-file input.txt --output-root converted-skills-marketplace --json
```

Local `run` and `convert` commands register a successful conversion in the standard personal Marketplace by default. The default file is auto-discovered, so do not add it with `codex plugin marketplace add`. Use `--no-register-personal` only for an intentionally workspace-only conversion. Registration makes the Plugin available in the Marketplace; it does not install or reinstall it. Use `--show-zip` only when you explicitly need the generated ZIP path in human-readable output; versioned JSON and the JSON conversion report retain artifact metadata for automation.

If packaging succeeds but personal registration fails, retry only the registration after addressing the reported permission, lock, or collision issue:

```bash
agent-skill-to-plugin register-personal \
  --plugin-dir converted-skills-marketplace/plugins/<plugin-name>
```

This does not resolve, package, install, or reinstall the Plugin again. Add `--force-personal` only after inspecting and explicitly authorizing replacement of divergent same-name personal state. The workspace-only `--force` flag does not authorize that home-directory replacement. A forced divergent update returns `status: "updated"`, `reinstall_required: true`, and `installation_performed: false`; reinstall the Plugin explicitly if an already-installed cached copy must pick up the new files.

## Supported inputs

The parser accepts a request in a code block, inline code, Markdown link, or surrounding prose. One Claude Marketplace-add command plus one Plugin-install command is treated as one logical request.

### `npx skills add`

```text
npx skills add vercel-labs/agent-skills --skill web-design-guidelines
```

```text
npx --yes skills@latest add https://github.com/vercel-labs/agent-skills/tree/main/skills/web-design-guidelines
```

`npx.cmd` and Bash, PowerShell, and cmd line continuations are accepted. Only the canonical `skills` package and a narrow option allowlist are accepted. User-provided global and agent targeting are removed; acquisition is copied into an isolated temporary project scope.

### GitHub repository or Skill path

```text
[https://github.com/mattpocock/skills](https://github.com/mattpocock/skills)
```

A repository root with multiple candidates returns `needs_selection`.

```text
https://github.com/mattpocock/skills/tree/main/skills/productivity/grill-me
```

A direct Skill directory or `SKILL.md` URL is selected without a question when it resolves to one valid Skill. Branches, tags, commit SHAs, encoded paths, and branch names containing `/` are resolved against Git refs rather than by fixed URL segment positions.

### Claude Plugin

```text
/plugin marketplace add mattpocock/skills
/plugin install mattpocock-skills@mattpocock
```

```text
claude plugin install skill-creator@claude-plugins-official
```

Marketplace resolution order is: an inline marketplace-add source, read-only `claude plugin marketplace list --json`, the built-in known-safe map, then a bounded public GitHub search validated against both Marketplace and Plugin names. If the result is not unique, the tool asks for the Marketplace repository or URL.

For a Claude Plugin, every valid Skill inside that Plugin boundary is included by default. Non-Skill Claude components are reported, not converted. Marketplace Plugin sources supported in 0.6.0 are relative paths, GitHub, Git, git-subdir, HTTPS archives, and npm registry packages. npm content is fetched from registry metadata and its tarball without invoking npm or lifecycle scripts. `command` sources are rejected.

### Local source

```text
C:\work\skills\my-skill
```

```text
./skills/my-skill
```

A local Skill directory, repository, `SKILL.md`, or supported archive can be used. Relative paths are resolved from `--source-base` (the current directory by default). A path that is also valid GitHub shorthand requires an explicit choice.

For a local repository, put `--output-root` outside that repository. The tool
rejects overlapping source/output boundaries before copying, so it cannot
recursively ingest its own resolution or generated artifacts.

### Other source forms

- GitHub shorthand such as `owner/repo`
- general Git URLs, including GitLab-style repository URLs
- one HTTPS `SKILL.md`
- ZIP, tar, tar.gz, and tgz sources

If unrelated acquisition sources appear in one request, the tool returns `needs_input`. It never silently merges them; choose one source or explicitly choose the offered combine-all action.

## Candidate selection

Selection is structural and deterministic. Exact files and directories are considered before nearby Plugin/Skill boundaries, common Skill layouts, and the full repository. A single valid candidate is automatic. Multiple plausible repository candidates require a user choice. Invalid front matter is retained in diagnostics instead of being silently dropped.

Claude Plugin imports are the deliberate exception: the Plugin is already the requested unit, so all valid Skills in that Plugin are selected together unless the caller explicitly narrows the selection.

The saved resolution contains a fixed commit when the source is Git-backed, or a content hash otherwise. `convert` verifies the snapshot hash before proceeding, so a later branch update cannot alter the selection turn.

## Generated output

The workspace keeps a reviewable conversion record:

```text
converted-skills-marketplace/
├── .agents/plugins/marketplace.json
├── plugins/<plugin-name>/
│   ├── .codex-plugin/plugin.json
│   ├── skills/<skill-name>/SKILL.md
│   ├── skills/<skill-name>/agents/openai.yaml # when generated or preserved
│   └── THIRD_PARTY_LICENSES/                 # when applicable
├── packages/<plugin-name>.zip
├── reports/<plugin-name>.json
├── reports/<plugin-name>.md
└── resolutions/
    ├── <resolution-id>.json
    └── <resolution-id>.snapshot/
```

For ordinary local `run` and `convert` operations, the validated Plugin is also registered at:

```text
~/plugins/<plugin-name>/
~/.agents/plugins/marketplace.json
```

The archive under `packages/` is deterministic provenance output. Human-readable CLI output omits its path unless `--show-zip` is explicitly supplied, and the Markdown report and Skill response do not surface it unless the user asks for a ZIP/archive, distribution bundle, or offline transfer. Versioned JSON output and the JSON conversion report retain its path and SHA-256 regardless of presentation mode.

Reports record normalized source information, requested and resolved refs, source and generated hashes, selected Skills, selection reasons, license evidence, external-reference handling, generated-copy compatibility adaptations, and compatibility/security diagnostics. License detection is evidence collection, not a legal conclusion.

The tool does not modify its fixed source snapshot and rechecks the snapshot hash before conversion. The generated copy is normally byte-preserving, with narrow metadata exceptions observed against the OpenAI Plugin validator bundled with the local Codex environment on 2026-08-29: it normalizes a `...` front-matter closer to `---`; if a source Skill uses `disable-model-invocation: true`, it sets that field to `false` in the generated `SKILL.md` and writes `policy.allow_implicit_invocation: false` to `agents/openai.yaml` to express explicit-only invocation intent. This policy's behavior still needs verification on each ChatGPT/Codex surface and version. Existing agent metadata is reduced, when needed, to the 0.6.0 conservative allowlist: interface display/description/icons/color/default prompt, `policy.allow_implicit_invocation`, and `dependencies.tools`. Default prompts are made to mention `$skill-name`, icon paths must resolve inside the Plugin, and added/removed/changed metadata field paths are recorded without copying their values. Every changed file, reason, source hash, and generated hash is recorded under `compatibility_adaptations` in both reports.

Existing workspace output names are not overwritten by default; `--force` is the explicit opt-in for replacing those workspace artifacts. Personal registration has a separate boundary: an identical registration is idempotent, while divergent same-name Plugin content or a conflicting Marketplace entry causes `output_conflict`. Only `--force-personal`, after inspecting the target and explicitly deciding to replace it, authorizes that personal-state replacement.

## Use the result in ChatGPT or Codex

The current OpenAI plugin specification requires `.codex-plugin/plugin.json`; skills are placed under `skills/<name>/SKILL.md`. A normal local conversion copies the validated Plugin to `~/plugins/<plugin-name>` and updates the standard personal Marketplace at `~/.agents/plugins/marketplace.json`. Codex discovers that file implicitly; do **not** run `codex plugin marketplace add` for this default path.

Registration only makes the Plugin available in the personal Marketplace. It does not install or reinstall it. The result records `installation_performed: false`; a forced update also records `reinstall_required: true`. Install or, when flagged, reinstall the generated Plugin explicitly from that Marketplace in a supported ChatGPT/Codex surface. Restart the desktop app if the entry does not appear, open a new chat, and invoke the imported Skill explicitly for the first test. Local Marketplace availability varies by surface. See the [official OpenAI plugin documentation](https://developers.openai.com/plugins/build/plugins) for the current product workflow.

Agent Skill to Plugin does not run `codex plugin add`, install or reinstall a Plugin, publish, or push anything. Its intended durable home-directory changes are limited to the registered Plugin directory and the standard personal Marketplace file. Registration is coordinated with an ownership-checked lock and recovery journal under the personal Marketplace root, plus staged or backup Plugin trees under the personal Plugin root. Those support files are normally removed, but a failed cleanup or rollback can deliberately retain them with diagnostics so a later run does not guess at ownership or delete unknown data. A Chat-only/cloud context cannot perform this local registration; use a local Desktop/Codex or CLI execution context instead.

## Security model

Important properties include:

- no `shell=True`; subprocesses receive argv arrays;
- imported scripts are never executed;
- `command` Plugin sources are rejected;
- npm packages are downloaded as registry tarballs, not installed;
- Git hooks and submodules are disabled during acquisition;
- URL credentials and dangerous shell syntax are rejected;
- HTTP redirects are revalidated, private/local targets are blocked, and production HTTPS connections are pinned to the public IP addresses that were validated while retaining certificate verification for the original hostname;
- archive entries, paths, symlinks, reparse points, collisions, size, count, and depth are bounded;
- existing external Skill references are copied only from the fixed snapshot; unresolved references are retained as explicit warnings, while escapes and unsafe targets fail;
- private keys, `.env`, and likely credential files are rejected;
- remote README, Skill text, and descriptions are never instructions to the resolver; and
- reports and process errors are sanitized before persistence.

These controls reduce acquisition and packaging risk. They do not prove that a Skill is benign when a user later invokes it. Review `SKILL.md`, bundled files, warnings, provenance, and upstream trust before installation. See [docs/security-model.md](docs/security-model.md) and [SECURITY.md](SECURITY.md).

## Licensing

The generated Plugin contains upstream material. Agent Skill to Plugin looks for repository LICENSE/COPYING/NOTICE files, Skill front-matter license metadata, Claude manifests, and resolver-provided license metadata, then records and may bundle that evidence. Absence of a detected license produces a redistribution warning. You remain responsible for confirming that the upstream terms permit your intended use and redistribution.

Agent Skill to Plugin itself is licensed under Apache-2.0; that license does not relicense imported Skills.

## Unsupported or intentionally excluded behavior

- Claude commands, agents, hooks, MCP servers, settings, LSP, live artifacts, monitors, and dependencies are not semantically converted.
- Claude `command` sources are never executed.
- Remote instructional content is not adapted or rewritten to replace product-specific instructions. Only the format-required OpenAI metadata normalization described above may change a generated copy.
- A static scan is not a behavioral sandbox or trust certification.
- Private repository access depends on credentials already configured in Git/SSH; credentials are not created or modified.

## Troubleshooting

`needs_selection` (exit 10): use the candidate `id` from the JSON response with `convert --select`. Do not delete the accompanying snapshot.

`needs_input` (exit 11): the source or Marketplace could not be chosen safely. Provide the requested repository/URL, or select one of the returned choices.

`dependency_missing`: install the executable required by that source type. GitHub/Git needs Git; npx input needs Node.js/npm/npx.

`authentication_failed`: verify your existing Git/SSH credentials outside the tool. Do not paste a token into a URL.

`security_rejected`: inspect the diagnostic. Bypasses are intentionally not provided for command sources, credential-bearing URLs, unsafe paths, symlinks, or secret-like files.

`output_conflict`: for a workspace artifact, use a different `--output-root`, remove the conflict after review, or pass workspace-only `--force` when replacement is intended. For divergent same-name content under `~/plugins` or a conflicting personal Marketplace entry, inspect the reported state and use the separate `--force-personal` only after explicitly authorizing that replacement. `--force` never authorizes the personal replacement.

If packaging completed but personal registration failed, keep the validated workspace Plugin and retry with `agent-skill-to-plugin register-personal --plugin-dir <generated-plugin-dir>` after resolving the reported permission, lock, journal, cleanup, or collision condition. A retained lock or recovery journal is evidence to inspect, not permission to delete it blindly. Use `--force-personal` only for an explicitly approved divergent-state replacement. Use `--no-register-personal` only when you intentionally want workspace artifacts without a personal Marketplace change.

For Windows paths containing spaces, use `--input-file` or quote the path. If a relative path is ambiguous, set `--source-base` explicitly.

For local repository input, an `output_conflict` whose relationship is
`destination_within_source` means the output root is inside the source tree.
Choose a sibling or other workspace directory outside that repository.

## Development

Run the offline test suite:

```bash
python -m unittest discover -s tests -v
```

The checked-in GitHub Actions workflow defines the same suite on Windows, Linux, and macOS. Live network smoke tests are optional and are not part of the default CI contract.

See [CONTRIBUTING.md](CONTRIBUTING.md), [docs/architecture.md](docs/architecture.md), and [docs/source-resolution.md](docs/source-resolution.md).

## Legacy entry point

The unpublished prototype interface remains as a compatibility wrapper:

```bash
python scripts/pluginize.py --command-file command.txt --output-root converted-skills-marketplace --json
```

New integrations should use `skill_to_plugin.py` or `agent-skill-to-plugin` and consume the versioned JSON envelope.
