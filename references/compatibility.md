# Runtime compatibility reference

The executable policy is centralized in `scripts/agent_skill_to_plugin/limits.py`. The JSON conversion report records the tool version, schema version, acquisition time, fixed commit or snapshot SHA-256, source file hashes, generated Plugin tree hash, and ZIP path/hash. Do not copy limits or transient dependency versions into workflow instructions.

## Source routing

- `npx skills add`: only the canonical unscoped `skills` package; user `--global` and `--agent` values are removed; npm lifecycle scripts, Git hooks, submodules, and shell evaluation are disabled.
- GitHub and Git: refs are enumerated, slash-containing refs use longest exact match, a commit is fixed, and content is materialized with `git archive` rather than checkout.
- Claude Plugin: inline Marketplace source, read-only official CLI JSON, a small known-safe mapping, then verified public search. Relative, GitHub, Git URL, git-subdir, HTTPS archive, and public npm-registry sources are supported. `command`, `headersHelper`, and custom npm registries are rejected.
- HTTP: public HTTPS only, bounded downloads, and every redirect target is revalidated. Direct single-file inputs cannot supply sibling resources.
- Archives and npm tarballs: entries are validated before extraction. npm registry tarballs are downloaded directly; `npm install`, `npm pack`, and lifecycle scripts are not invoked.
- Local paths: the exact filesystem state is copied into the resolution snapshot. A Git commit and dirty-state flag are recorded when detectable, but uncommitted content is pinned by the snapshot hash.

## Fixed two-stage resolution

`resolve` writes `resolutions/<id>.json` and a sibling snapshot. `convert` performs no source resolution or network acquisition. It requires the recorded resolution path to stay under the output root, rejects duplicate JSON keys, rehashes the snapshot, rebuilds every candidate, and fails if any persisted identity changed.

## Security and packaging policy

The current local policy rejects links and reparse points, special files, traversal and absolute archive paths, path collisions after case and Unicode normalization, file/directory collisions, control characters, Windows-reserved names, trailing dots or spaces, likely credential files, private keys, duplicate Skill names, and identity overflow.

Configured limits at version 0.6.0 are 5,000 entries, 100 MiB per member, 512 MiB total expanded data, 100 MiB downloaded or compressed data, 20 path levels, and 1,024 path characters. The generated ZIP has one top-level Plugin directory and deterministic member metadata.

Local `run` and `convert`, including the legacy wrapper, register successful results at `~/plugins/<plugin-name>` in `~/.agents/plugins/marketplace.json` by default. That standard personal Marketplace is discovered implicitly; do not call `codex plugin marketplace add` for it. Registration does not run `codex plugin add`, install or reinstall the Plugin, or publish anything. `--no-register-personal` is the explicit workspace-only opt-out. Divergent personal state requires separately authorized `--force-personal`; workspace `--force` does not grant that authority. After packaging succeeds but registration fails, retry only registration with `register-personal --plugin-dir <generated-plugin-dir>`. Human output shows the generated ZIP only with `--show-zip`; the versioned JSON result and JSON conversion report retain its artifact metadata, while the Markdown report and Skill response surface it only after an explicit ZIP, archive, distribution, or offline-transfer request.

Personal registration coordinates its two durable targets with an ownership-checked lock and recovery journal. Cleanup and rollback are best effort across filesystem operations; if the diagnostic reports retained lock, journal, stage, or backup state, inspect it and follow the returned retry condition instead of deleting it by guessed ownership.

These controls reduce acquisition and packaging risk. They do not prove that imported instructions are trustworthy or that a license permits redistribution.

## Stable error categories

JSON responses distinguish unknown input, source, Marketplace, and Plugin; no candidates; authentication and network failures; invalid manifests; unsupported sources; security rejection; package validation; missing dependencies; invalid selection; resolution-integrity failure; output conflicts; and personal-Marketplace registration failures. Use `status` and `error_code`, not human message text, for automation.

The historical `scripts/pluginize.py --command-file ...` spelling remains as a compatibility wrapper for `npx skills add` requests. New callers should use `scripts/skill_to_plugin.py run|resolve|convert`.
