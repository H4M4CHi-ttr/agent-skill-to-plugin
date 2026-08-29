# Security model

## Scope and trust boundary

Agent Skill to Plugin is a local acquisition and packaging tool. User-provided request text, remote repositories, archives, npm packages, Marketplace manifests, `SKILL.md`, README files, filenames, Git output, HTTP responses, and subprocess stderr/stdout are untrusted.

The trusted computing base includes the tool source, Python runtime and PyYAML, required local executables, operating-system filesystem semantics, and the user's preconfigured Git/SSH trust decisions. A compromised Git, Node, npx, Claude CLI, Python, certificate store, or host is outside the tool's containment guarantee.

## Protected assets

- files outside the chosen output and temporary directories;
- user home and existing Skill/Plugin installations;
- Git/SSH/npm/Claude credentials and private repository data;
- integrity of the source snapshot across a multi-turn selection;
- accuracy of generated provenance and hashes; and
- absence of arbitrary code execution during import.

## Threats and controls

### Prompt or instruction injection from source content

Repository prose and Skill instructions are parsed only as data needed for candidate metadata and packaging. Resolution relies on command grammar, URL structure, Git refs, manifests, and filesystem boundaries. The tool does not follow “run this installer” or similar text.

### Shell and argument injection

Subprocesses receive argv arrays and `shell=False`; stdin is disabled for acquisition commands. The npx parser uses a strict executable/package/subcommand/option grammar, rejects shell metacharacters and unknown options, and renders sensitive option/value pairs in a non-smuggleable form. User global/agent routing is not forwarded.

On Windows, `.cmd` launchers are not passed to a shell; the npx fetcher locates the Node CLI script and invokes it through `node`. Claude Marketplace discovery declines batch launchers rather than silently using a command shell.

### Imported code execution

No scripts from an imported Skill are executed. Claude `command` sources are rejected. npm Plugin sources use registry metadata and download the published tarball directly; `npm install` and lifecycle scripts are not invoked. Git hooks and submodules are disabled by default.

### Filesystem and archive escape

Paths are normalized and checked against a fixed root. Absolute paths, traversal, control characters, Unicode-normalization collisions, case-insensitive collisions, file/directory collisions, Windows reserved names, illegal trailing dots/spaces, symbolic links, special files, and Windows reparse points are rejected as applicable.

ZIP/tar extraction validates members before committing them and enforces centralized limits for file count, total size, compressed/member size, depth, and path length. Generated ZIPs use POSIX entry names and contain one top-level Plugin directory.

### Secret collection or disclosure

URL userinfo/passwords and sensitive token-like query parameters are rejected. Static validation rejects private keys, `.env`, and likely credential files. Process output and diagnostics are bounded and sanitized before persistence. Authentication comes from existing Git/SSH configuration; the tool does not ask users to paste tokens into a reportable URL.

Secret detection is heuristic and cannot find every proprietary format. Users must still inspect private-source output before sharing it.

### SSRF and unsafe redirects

HTTP acquisition requires HTTPS for applicable source types. Initial and
redirected URLs are validated, redirect counts and response sizes are bounded,
and loopback, link-local, private, metadata, or otherwise prohibited network
destinations are rejected. The production transport connects to the exact
public numeric addresses returned by that validation and uses the original
hostname for TLS SNI/certificate verification, preventing a second DNS lookup
from rebinding the request to a private target. Environment proxy variables are
not used by this pinned transport; separately audited proxy support is outside
the 0.5.0 contract. Network sandboxing remains recommended for defense in depth.

### Time-of-check/time-of-use substitution

Git refs are resolved to commits before acquisition. Other sources receive an artifact/tree hash. Candidate selection is persisted with the fixed snapshot. Conversion recomputes and compares the snapshot hash rather than re-fetching a branch.

The user can still deliberately replace tool code, interpreter behavior, or files while the process is running; protection against a hostile local administrator is out of scope.

### Partial or destructive output

Packaging occurs in a staging directory. Trees, manifests, ZIP contents, and size are validated before final placement. Existing artifacts are not silently overwritten. `--force` is a deliberate caller authorization and should be used only after inspecting the target.

## Validation policy

The current centralized policy includes conservative limits for file count, byte size, archive member size, path depth/length, HTTP redirects, and report text. These values are local defense-in-depth settings, not all documented OpenAI maximums. See `scripts/agent_skill_to_plugin/limits.py` for the executable policy.

## Generated-copy metadata adaptation

Remote instructions are never rewritten or executed. Packaging may mechanically normalize format metadata in the generated copy: a `...` front-matter closer becomes `---`; `disable-model-invocation: true` becomes `false`, while `policy.allow_implicit_invocation: false` expresses explicit-only invocation intent. Existing `agents/openai.yaml` fields outside the conservative 0.5.0 allowlist are omitted, default prompts must name the Skill token, and icon paths must stay inside the Plugin. The tool leaves the fixed source snapshot unchanged and revalidates its hash; changed field paths, source/generated hashes, and reasons are included in the report so these exceptions cannot be silent. Invocation behavior still requires verification on each product surface and version.

Front matter uses PyYAML safe loading with duplicate/shape/type checks around the accepted manifest. A parse failure is reported with its path; it is not silently ignored.

External relative references from selected `SKILL.md` files are resolved only inside the acquired snapshot. Existing safe targets can be copied by an explicit plan and are recorded with source/destination hashes. Missing targets are reported as warnings because they may be runtime outputs, template placeholders, or repository routes. Escaping, unsafe, or colliding targets fail rather than producing an unsafe Plugin.

## What a successful conversion means

A successful result means:

- acquisition and packaging completed under the implemented policy;
- the selected `SKILL.md` manifests were structurally valid;
- the packaged files passed static path/content checks; and
- the recorded hashes matched the generated artifacts.

It does not mean:

- the upstream publisher is trustworthy;
- the Skill will behave safely when an AI follows it;
- every secret or vulnerability was detected;
- the Skill is compatible with every ChatGPT/Codex surface; or
- redistribution rights were established.

## Operational recommendations

- Prefer commit-pinned and well-maintained upstream sources.
- Review `SKILL.md`, scripts, references, license evidence, and every warning.
- Use a network-restricted environment when processing unknown sources.
- Keep the JSON report and SHA-256 alongside a distributed ZIP.
- Test a new Plugin in a new, low-privilege chat before broader use.
- Do not pass `--force` in unattended automation without a separate output-retention policy.

See [SECURITY.md](../SECURITY.md) for private vulnerability reporting.
