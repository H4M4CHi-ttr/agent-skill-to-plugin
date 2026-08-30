# Contributing

Thank you for helping improve Agent Skill to Plugin. Security boundaries and deterministic behavior matter more than broad but ambiguous source support.

## Before opening a change

- Use an issue or discussion for a new source type, manifest interpretation, or behavior that could change security or selection semantics.
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
- Do not add live credentials, copied private repositories, or third-party fixtures without redistribution permission.

## Development setup

```bash
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

On Windows PowerShell, use `.venv\Scripts\python.exe` in place of `.venv/bin/python`.

Before a release, build the Python distributions and run the deterministic Skill-archive packaging check:

```bash
.venv/bin/python -m build
.venv/bin/python -B scripts/build_skill_zip.py --output ../agent-skill-to-plugin-ci-check.zip
```

The Skill archive is an internal CI invariant check only. Do not publish or attach it as a release asset. End users install Agent Skill to Plugin with `npx skills add H4M4CHi-ttr/agent-skill-to-plugin`; generated Plugin ZIPs remain converter outputs.

The preferred end-user runtime is `uv`; the Python fallback supports Python 3.10+ and PyYAML is the only Python runtime dependency in 0.6.0. Keep the PEP 723 metadata in `scripts/skill_to_plugin.py`, `pyproject.toml`, and `requirements.txt` synchronized. Git, npx, and Claude CLI are source-specific external tools and must not become unconditional test requirements.

## Design rules

- Parse untrusted text into typed models before acquisition.
- Add a focused Resolver/Fetcher instead of growing one source-type condition tree.
- Never interpret README, comments, descriptions, or Skill instructions as resolver commands.
- Use argv arrays with `shell=False`; imported scripts and package lifecycle scripts must not run.
- Preserve source bytes unless a bounded compatibility or path operation is explicitly designed and reported.
- Make candidate selection structural, deterministic, and explainable.
- Pin Git sources to a commit and other sources to an artifact hash before asking for a later-turn selection.
- Return structured diagnostics for malformed candidates; do not silently omit them.
- Centralize safety and compatibility limits rather than scattering numbers through the code.
- Do not weaken a rejection merely to make a fixture pass.

## Tests

Every bug fix or new input form should include an offline test. Prefer fixtures, an in-process HTTP server, fake subprocess runners, and local Git repositories. Default CI must not depend on mutable public repositories.

At minimum, run:

```bash
python -m unittest discover -s tests -v
python -m compileall -q scripts
```

Cross-platform changes should cover Windows path rules, POSIX paths, Unicode normalization, archive member paths, and ZIP entry separators where relevant. Optional live smoke tests must be clearly marked and excluded from the default suite.

## Fixtures

Keep fixtures small, synthetic, and licensed for this repository. Unsafe fixtures should be generated in a temporary test directory when possible, so the repository itself does not contain an accidental secret or active link. Never commit a real private key, even as a security fixture.

## Documentation and compatibility

Update `README.md`, `README.ja.md`, and the relevant design document when user-visible behavior changes. Update `CHANGELOG.md` for release-facing changes. Time-sensitive OpenAI format facts belong in [docs/compatibility.md](docs/compatibility.md) with a verification date and primary source.

The JSON `schema_version`, exit codes, saved resolution shape, and legacy wrapper are compatibility surfaces. Changes require migration notes and tests.

## Pull requests

Keep changes focused. Describe:

- the source form or security property being changed;
- deterministic resolution/selection rules;
- new failure modes and exit codes;
- tests run and operating systems actually verified; and
- any claims that remain unverified.

Do not publish packages, register marketplaces, install generated Plugins, or modify user home directories as part of tests.
