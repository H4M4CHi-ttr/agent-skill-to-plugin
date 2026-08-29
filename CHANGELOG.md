# Changelog

All notable changes to Agent Skill to Plugin are documented here. The project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and intends to use Semantic Versioning after the public API stabilizes.

## [Unreleased]

No changes yet.

## [0.5.0] - 2026-08-29

### Added

- Extensible parser, resolver, fetcher, discovery, selection, validation, packaging, provenance, and reporting modules.
- Inputs for `npx skills add`, Claude Plugin install requests, GitHub paths, Git URLs, local paths, single `SKILL.md` URLs, and common archives.
- Two-phase `resolve` and `convert` flow with content-addressed, persistent snapshots.
- Claude Marketplace resolution through an inline source, read-only Claude CLI JSON, a known-safe map, and validated bounded public search.
- Relative, GitHub, Git, git-subdir, HTTPS archive, and lifecycle-free npm Claude Plugin sources; explicit rejection of command sources.
- Deterministic candidate selection and default all-Skill behavior within one Claude Plugin boundary.
- Versioned JSON output, structured errors, and stable exit-code categories.
- Provenance, license evidence, compatibility diagnostics, deterministic ZIPs, and JSON/Markdown reports.
- Reported generated-copy metadata adaptation for source Skills that use `disable-model-invocation: true`: the locally validated copy uses `false` and expresses explicit-only intent through `agents/openai.yaml` policy without changing the fixed source snapshot.
- Normalized the locally rejected `...` front-matter closer, validated icon assets, required default prompts to mention the Skill token, and reported agent metadata changes at field-path granularity without recording values.
- Changed unresolved relative references from a false-positive-prone hard failure to an explicit report warning; snapshot escapes, unsafe targets, and copy collisions still fail closed.
- Offline fixtures and automated tests, plus a cross-platform GitHub Actions workflow definition for Python 3.10, 3.13, and 3.14.
- Deterministic release-Skill ZIP builder with single-root and cache-exclusion validation.
- English and Japanese documentation for public beta use.

### Security

- Preserved and expanded argv-only process execution, option allowlists, secret/path detection, archive extraction limits, symlink/reparse-point rejection, network target validation, and output sanitization.
- Disabled Git hooks and submodules by default.
- Added npm registry tarball retrieval without invoking npm install or lifecycle scripts.
- Bound production HTTPS connections to prevalidated public IP addresses while preserving TLS verification for the original hostname, closing the DNS-rebinding gap between validation and connection.
- Rejected overlapping local source/output boundaries, pinned Marketplace Git `sha` declarations, and added early archive-entry limits plus whole-file private-key scanning.

### Changed

- Adopted **Agent Skill to Plugin** / `agent-skill-to-plugin` as the public identity to reflect source types beyond npx, align the display, package, CLI, import, and Skill names, and avoid collision with an existing Agent Skill Porter project.
- Pinned GitHub Actions to immutable commit SHAs for the initial public release.
- Added cross-platform wheel installation checks and repository-wide LF normalization for reproducible release inputs.
- Kept an explicit local Claude Marketplace directory as the snapshot boundary instead of widening it to an enclosing Git repository.
- Split the unpublished approximately 1,400-line prototype script into focused modules. `scripts/pluginize.py` remains as a compatibility wrapper.
- Reset the version from the prototype's unpublished `1.0.0` label to `0.5.0`. No public stable release was downgraded; the 0.x version communicates that resolver coverage and integration contracts are still beta.

### Known limitations

- Non-Skill Claude components are detected but not semantically converted.
- The narrow front-matter adaptation handles an ordinary top-level scalar `disable-model-invocation` line. YAML merge/anchor/flow forms fail closed instead of being broadly rewritten.
- Existing `agents/openai.yaml` fields outside the conservative 0.5.0 allowlist are omitted from the generated copy and reported.
- `policy.allow_implicit_invocation: false` expresses explicit-only intent but is not a cross-surface runtime guarantee; verify it on the target ChatGPT/Codex version.
- Static validation cannot certify the behavior of a Skill when later invoked.
- Availability of local/repository marketplaces depends on the OpenAI product surface.
- Live public-source behavior depends on external services, authentication, rate limits, and installed CLI versions; normal tests use local fixtures and mocks.
