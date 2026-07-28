# Contributing to GoLogs

## Quick Links

- 🐛 **Report a Bug**: [Open Bug Report](.github/ISSUE_TEMPLATE/bug_report.yml)
- ✨ **Propose a Feature**: [Open Feature Request](.github/ISSUE_TEMPLATE/feature_request.yml)
- 🛡️ **Security Policy**: Please review our [SECURITY.md](SECURITY.md) before reporting security issues.

## Branching

Trunk-based, short-lived branches: `feature/<slug>`, `fix/<slug>`,
`security/<slug>`. `main` is always releasable.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/): `feat:`,
`fix:`, `docs:`, `test:`, `refactor:`, `chore:`, `security:`. The release
workflow generates changelogs from this history — see
`.github/workflows/release.yml`.

## PR checklist

CI (`.github/workflows/ci.yml`) must be green: lint, unit tests,
integration tests, security scan, frontend build, OpenAPI contract check,
container build + scan.

## Solo-project self-review checklist

This is a single-maintainer project, so there's no second engineer to
open a pull request against. Before merging any change to `main`,
complete at least one of the following as a substitute for peer review —
pick whichever is most relevant to the change:

- [ ] **Re-read the diff cold** (close the editor, come back in a few
      minutes, review as if it were someone else's PR)
- [ ] **Trace the change against the PRD** — does it match the relevant
      section's spec, or does it introduce drift that needs documenting
      as a deliberate decision (see `docs/context_transfer.md` §7/§7b for
      the format used so far)?
- [ ] **Run the specific test file for the changed module and read the
      failure output before it passes**, not just the final green
      checkmark — a test that was never seen to fail is weaker evidence
      than one that was
- [ ] **For anything touching `orchestrator/` or `mcp_server/`**: re-read
      the change against the threat model (PRD §20) — does this introduce
      a new trust boundary crossing, and if so, is it gated the same way
      every other one is (deny-list + confirmation + injection scan)?
- [ ] **For anything touching `core/`**: confirm the import-linter
      contract still passes (`core` has zero dependency on
      `orchestrator`/`api`/`mcp_server`) — this is enforced by CI, but
      worth checking locally first: `cd backend && pip install -e ".[dev]" && lint-imports`

## Code quality gates (all enforced in CI, all runnable locally)

```bash
cd backend
ruff check .          # lint
ruff format --check . # formatting
mypy --strict app/    # types
pytest tests/unit --cov=app --cov-fail-under=80
pytest tests/integration
bandit -r app/ -c pyproject.toml -ll
pip-audit
```

```bash
cd frontend
npm run lint
npm run typecheck
npm run build
```

## Versioning

Semantic Versioning. `0.x.y` until FR-1 through FR-12 are all complete;
`1.0.0` at the first clean pass of the Production Checklist
(`docs/KNOWLEDGE_BASE.md` → Production Checklist).
