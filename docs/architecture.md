# Architecture

## Goals

Agent Skill to Plugin bridges heterogeneous Agent Skill sources into one OpenAI skills-only Plugin while retaining a reviewable source trail. The design separates source-specific acquisition from shared Skill discovery, validation, selection, packaging, and personal-Marketplace registration.

Non-goals include synthesizing Skills from Claude commands/agents, executing installers, deciding semantic preferences with opaque AI heuristics, publishing a Plugin, or proving that imported instructions are benign.

## Processing pipeline

```text
untrusted request text
        │
        ▼
 input_parser ──► ParsedInput
        │
        ▼
 ResolverRegistry ──► source-specific Resolver / Fetcher
        │
        ▼
 immutable snapshot + ResolvedSource
        │
        ▼
 discovery ──► SkillCandidate[] + diagnostics
        │
        ▼
 selection ──► automatic | needs_selection | selected Skills
        │                       │
        │                       └── persisted ResolutionState and snapshot
        ▼
 validation + compatibility + provenance
        │
        ▼
 packaging ──► Plugin, workspace marketplace, deterministic ZIP, reports
        │
        ▼
 personal registration ──► ~/plugins/<name> + ~/.agents/plugins/marketplace.json
```

Each boundary consumes structured data. Repository prose is never fed back as instructions to parsing or resolution.

## Modules

`scripts/skill_to_plugin.py` is the source-tree CLI shim. The installed `agent-skill-to-plugin` command calls the same `agent_skill_to_plugin.cli:main` entry point. `scripts/pluginize.py` is a compatibility wrapper for the unpublished npx-only prototype.

`input_parser.py` extracts one logical request from prose/Markdown, validates command grammar, deduplicates equivalent mentions, and returns `ParsedInput` or `NeedsInputError`.

`resolver_registry.py` dispatches a typed input to focused resolvers. It also handles an explicitly authorized multi-source composition by resolving children into separate snapshot subdirectories.

`resolvers/` controls source semantics: npx, Claude Marketplace/Plugin, GitHub, Git, local files, HTTP Skill manifests, and archives. A resolver returns a `ResolvedSource` without exposing acquisition-specific conditions to packaging.

`fetchers/` performs bounded transport or extraction: Git, GitHub ref metadata, HTTP, archive, npm registry, and npx project acquisition. Fetchers do not discover or select Skills.

`discovery.py` finds `SKILL.md` candidates using exact-path, nearby-boundary, common-layout, and repository-wide phases. It preserves invalid candidates with parse diagnostics.

`selection.py` applies deterministic structural policy. It never ranks candidates by inferred semantic preference. A Claude Plugin boundary selects all valid Skills by default.

`validation.py` validates manifests, directory contents, paths, collisions, secret-like material, and relative references. External references are converted into explicit copy plans or diagnostics.

`compatibility.py` statically identifies Claude/Anthropic product references and non-Skill Claude components. It reports but does not rewrite instructional content. Packaging has narrow format-level exceptions for front-matter delimiters and OpenAI compatibility metadata, while the fixed source snapshot and source hashes are retained and every adaptation is reported.

`provenance.py` records source identity, timestamps, hashes, selection reasons, and license evidence. License evidence is not interpreted as a legal grant.

`packaging.py` builds a staged Plugin, validates its manifest and tree, creates a deterministic ZIP, updates the workspace marketplace, emits reports, and commits outputs. It reduces partial workspace artifacts by staging first. The ZIP remains available for provenance and explicit distribution requests. Ordinary human output shows it only with `--show-zip`; versioned JSON and the JSON conversion report retain its path and SHA-256, while the Markdown report does not surface it.

`personal_marketplace.py` registers a validated result in the standard personal locations: `~/plugins/<plugin-name>` and `~/.agents/plugins/marketplace.json`. It preserves unrelated Marketplace metadata and entry order, treats an identical registration as idempotent, rejects divergent same-name content by default, and requires the separate `--force-personal` authorization for replacement. An ownership-checked lock and recovery journal coordinate the Plugin-tree and Marketplace-file updates; staged and backup trees support best-effort cleanup or rollback. If recovery cannot complete safely, attributable lock, journal, or backup state is retained with diagnostics instead of claiming cross-file atomicity or deleting unknown data. The standard personal Marketplace is discovered implicitly; registration does not invoke `codex plugin marketplace add`, install, or reinstall the Plugin.

`models.py` contains serialization-friendly data classes including `ParsedInput`, `ResolvedSource`, `MarketplaceInfo`, `PluginSource`, `SkillCandidate`, `ResolutionState`, `SelectedSkill`, `ConversionResult`, personal registration results, `Provenance`, and `Diagnostic`.

`limits.py` is the single policy location for tool/schema versions and resource/format limits. Those values are a conservative tool policy; they are not all claims about hard product limits.

## Two-phase operation

`resolve` writes a resolution JSON and snapshot below `output-root/resolutions/`. Git resolvers record the fixed commit; other resolvers record a tree or archive hash. Candidate IDs are derived from the fixed candidate identity.

If selection is needed, the caller asks the user once. `convert` loads the saved state, verifies that the snapshot is still within the expected location and has the recorded hash, resolves the explicit selector, then packages. It does not re-fetch a moving branch.

`run` composes both phases: it converts immediately only when the deterministic selection policy returns a complete choice. Successful local `run` and `convert` operations register by default; `--no-register-personal` is the explicit workspace-only opt-out. If packaging succeeds but registration fails, `register-personal --plugin-dir <generated-plugin-dir>` retries only registration. `--force-personal` authorizes divergent personal-state replacement independently of workspace `--force`. `--show-zip` affects ordinary human presentation, not deterministic archive generation or the ZIP metadata retained in versioned JSON and the JSON conversion report.

## Plugin boundary

The generated artifact is intentionally skills-only:

```text
<plugin>/
├── .codex-plugin/plugin.json
└── skills/
    └── <skill>/
        ├── SKILL.md
        └── ...
```

Safe source-relative files inside a selected Skill are preserved. Validated external references can be copied into a stable plugin-relative location and are recorded in the report. Detected license/notice files may be included under `THIRD_PARTY_LICENSES/` with their origin recorded.

Packaging normalizes a `...` front-matter closer to `---`. When source front matter contains `disable-model-invocation: true`, it changes only the generated front-matter value to `false` and expresses the source intent as `policy.allow_implicit_invocation: false` in `agents/openai.yaml`. Existing agent metadata is filtered through the tool's conservative 0.6.1 allowlist; default prompts must mention `$skill-name`, icon paths must resolve within the Plugin, and changed field paths are reported without their values. These are mechanical, reportable format adaptations, not semantic rewrites of the Skill instructions. Runtime invocation behavior remains product-surface and version dependent.

## Extension points

To add a source:

1. extend parsing only enough to create a typed `ParsedInput`;
2. implement transport in a Fetcher when reusable;
3. implement a Resolver that produces an immutable `ResolvedSource`;
4. register deterministic dispatch without leaking source-specific fields downstream;
5. add offline fixtures for success, ambiguity, malformed input, and security rejection; and
6. document provenance and authentication behavior.

A new source must not bypass the common discovery, validation, selection, and packaging pipeline.

## Compatibility surfaces

- JSON envelope `schema_version`
- structured error codes and process exit codes
- serialized ResolutionState and snapshot integrity contract
- CLI subcommands and core option names
- default personal-registration behavior and its explicit opt-out
- separate workspace `--force` and personal-registration `--force-personal` authorization
- standalone `register-personal` retry behavior
- legacy `pluginize.py` arguments while the compatibility wrapper is supported

Version 0.6.1 is beta, so changes are possible, but intentional migration notes and regression tests are required.
