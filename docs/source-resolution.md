# Source resolution

## Principle

Resolution answers two mechanical questions: “what exact bytes were requested?” and “what structural Skill boundaries exist in those bytes?” It does not ask remote prose what to run and does not guess which Skill the user would prefer.

## Logical request parsing

The parser normalizes Markdown code fences, inline code, Markdown links, normal prose, and Bash/PowerShell/cmd line continuations. Equivalent mentions of the same source are deduplicated. A Claude Marketplace-add plus Plugin-install pair is one request.

Inputs containing multiple unrelated acquisition sources return `needs_input`. The response offers the detected sources and, where appropriate, an explicit combine-all choice. Composition happens only after that authorization and preserves a separate child snapshot for each source.

Rejected before resolution:

- pipes, redirections, chaining, command substitution, or additional commands;
- an npm executable/package other than canonical `npx`/`npx.cmd` and `skills`;
- unknown or malformed npx options and option-looking values;
- credentials or sensitive token-like query parameters embedded in URLs; and
- malformed control characters or ambiguous command grammar.

## Resolution table

| Input kind | Acquisition | Fixed identity | Notes |
|---|---|---|---|
| `npx skills add` | isolated project-scoped resolution directory, argv-only npx execution, temporary npm cache/config | copied snapshot tree hash | user global/agent targets are removed; imported scripts are not run |
| GitHub repository/path | Git ref enumeration and fixed commit fetch | commit SHA + tree hash | longest valid ref match handles branch names containing `/` |
| general Git URL | `git ls-remote` then fixed commit `git archive` | commit SHA + tree hash | no checkout, submodules, or hooks |
| local directory/file | validated filesystem copy | tree hash | relative to `--source-base` |
| HTTPS `SKILL.md` | bounded HTTP fetch | response/file hash | redirects and destination addresses are revalidated |
| ZIP/tar/tar.gz/tgz | bounded download/copy and safe extraction | archive hash + tree hash | content type is not trusted as the only signal |
| Claude Plugin | Marketplace manifest followed by Plugin source resolver | underlying commit or artifact hash | Plugin boundary becomes selection scope |
| npm Plugin source | registry metadata plus published tarball | version, integrity evidence, archive/tree hashes | npm executable and lifecycle scripts are not used |

## GitHub paths and refs

Repository-root, `tree`, `blob`, and raw URLs are accepted. Query and fragment text is removed during normalization where it is not part of source identity. Encoded path segments are decoded for validation.

For a `tree/<ref>/<path>` or `blob/<ref>/<path>` URL, the tool enumerates actual remote heads/tags and chooses the longest valid ref prefix. It then resolves that ref to a commit and acquires the fixed commit. A default branch need not be named `main`.

If public API metadata is unavailable or unsuitable, Git remains the authoritative fallback. Private repositories use the user's existing Git/SSH configuration; the tool does not create credentials, edit credential helpers, or persist secrets in reports.

## Path-oriented discovery order

After acquisition, discovery considers:

1. the requested file or directory itself;
2. `SKILL.md` directly in that directory;
3. the nearest ancestor Skill or Claude Plugin boundary;
4. Skills below the requested path;
5. common layouts such as `skills/`, `.agents/skills/`, `.claude/skills/`, and `plugins/*/skills/`; and
6. the remaining repository.

Exactness affects the documented `selection_reason`; it does not authorize semantic guessing. One valid candidate is automatic. Several plausible candidates produce `needs_selection`.

Malformed front matter remains visible as an invalid `SkillCandidate` with its path and parse diagnostic. Invalid candidates cannot be packaged, but they are not silently omitted from the resolution report.

## Claude Marketplace resolution

For `plugin@marketplace`, resolution follows this order:

1. an accompanying `/plugin marketplace add` or `claude plugin marketplace add` source;
2. `claude plugin marketplace list --json`, read-only and only when a directly executable Claude CLI is available;
3. the built-in known Marketplace map (`claude-plugins-official` in 0.5.0);
4. a bounded public GitHub code search; every candidate is fetched and its `.claude-plugin/marketplace.json` must match both the Marketplace and Plugin names; and
5. `needs_input` asking for a repository or URL.

No Claude cache database or undocumented internal cache JSON is read. Search failure is not treated as proof that a Marketplace does not exist.

The Marketplace manifest must contain one exact Plugin entry. Supported source forms are:

- relative Marketplace-repository paths, including `./`;
- GitHub repositories;
- general Git repositories;
- Git repositories with a validated subdirectory;
- HTTPS archives; and
- npm registry packages acquired from metadata/tarballs without installation.

`command` is a security rejection, never an implicit opt-in.

## Snapshot and resume contract

The initial resolution creates:

```text
<output-root>/resolutions/<resolution-id>.json
<output-root>/resolutions/<resolution-id>.snapshot/
```

The JSON records the normalized source, requested ref/path, resolved commit when available, snapshot hash, candidates, diagnostics, and selection policy. Before conversion, the tool recalculates the snapshot hash and rejects modifications. The snapshot is therefore part of the auditable input and must be retained until conversion finishes.

## Failure taxonomy

The CLI distinguishes unknown input/source/Marketplace/Plugin, no candidates, ambiguity, authentication, network failure, invalid manifests, unsupported sources, security rejection, package validation, missing dependencies, invalid selection, resolution integrity failure, and output conflict. JSON responses always include `schema_version`, `status`, and `error_code`.
