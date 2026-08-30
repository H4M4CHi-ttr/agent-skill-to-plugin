# Security policy

Agent Skill to Plugin acquires and packages untrusted third-party files. Security reports are welcome and should be handled privately.

## Supported versions

During public beta, only the latest 0.5.x release receives security fixes. Older beta and unpublished prototype builds are unsupported.

## Reporting a vulnerability

Do not open a public issue for a vulnerability or include live credentials, private repository content, or weaponized payloads in public channels.

Use the repository's **Security → Report a vulnerability** form (GitHub private vulnerability reporting) when enabled. If it is unavailable, contact the repository maintainers privately through the repository owner's established security contact. Include:

- affected Agent Skill to Plugin version and commit;
- operating system and runtime path (`uv` plus resolved Python version, or direct Python version);
- source type and the smallest safe, synthetic reproducer;
- expected and observed security boundary;
- impact and whether the issue requires user interaction; and
- suggested mitigation, if known.

Replace secrets with unmistakable placeholders. A fixture that reproduces the file structure is preferable to a link containing sensitive material.

Maintainers should acknowledge a complete report within seven calendar days when possible. Triage and remediation timing depends on severity and reproducibility; no fixed disclosure deadline is promised during beta. Coordinate public disclosure with the maintainers.

## In scope

- command or lifecycle execution during acquisition or packaging;
- shell injection or option smuggling;
- credential disclosure in logs, reports, URLs, or subprocess output;
- archive traversal, symlink/reparse-point escape, or filesystem boundary escape;
- SSRF or unsafe redirect handling;
- bypasses of file count, size, depth, or path-collision limits;
- snapshot integrity or resolution-resume substitution;
- silent or unsafe generated-copy metadata adaptation, including loss of the
  requested explicit-invocation boundary;
- generated ZIP or marketplace path traversal; and
- unsafe overwrite behavior.

## Usually out of scope

- malicious instructions that execute only after a user deliberately installs and invokes an imported Skill, unless the tool claimed to remove or sandbox them;
- availability failures caused solely by an external registry, Git host, rate limit, or unsupported upstream manifest;
- license disputes; and
- findings that require credentials placed directly in an input URL despite the documented prohibition.

Out-of-scope items may still justify a documentation or hardening improvement.

## Security guarantees and non-guarantees

The tool does not execute imported scripts and treats repository text as data. It performs static checks and produces provenance. It is not a sandbox, malware scanner, legal review, or trust certificate. A successful conversion means the files met the implemented packaging policy; it does not mean the Skill is safe to invoke.

The tool leaves the fixed source snapshot unchanged and verifies its hash
again before conversion. A generated copy may receive narrow, format-level
adaptations: a `...` front-matter closer becomes `---`, and an ordinary
top-level `disable-model-invocation: true` scalar is changed to `false`, while
`policy.allow_implicit_invocation: false` is emitted to express the source's
explicit-only intent. Existing agent metadata is filtered through the
documented 0.5.0 conservative allowlist. Default prompts must name the Skill
token and icon paths must resolve inside the generated Plugin. Every adapted file, reason, source
hash, and generated hash must appear in `compatibility_adaptations`; the
operation fails closed when it cannot make this narrow change safely. The
policy field is an intent declaration, not a guarantee of identical runtime
behavior across every ChatGPT/Codex surface and version.

See [docs/security-model.md](docs/security-model.md) for the threat model and control inventory.
