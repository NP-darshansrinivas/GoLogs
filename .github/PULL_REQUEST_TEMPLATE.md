## Summary of Changes

Provide a clear and concise summary of the changes introduced in this Pull Request.

- Related Issue / Spec: # (if applicable)

## Type of Change

- [ ] 🐛 Bug fix (non-breaking change which fixes an issue)
- [ ] ✨ New feature (non-breaking change which adds functionality)
- [ ] 🛡️ Security / Hardening enhancement
- [ ] ♻️ Refactoring / Performance improvement
- [ ] 📝 Documentation update

## Checklist

- [ ] Code follows the project's styling and linting guidelines (`ruff check .` / `npm run lint`).
- [ ] Type annotations pass cleanly without errors (`mypy --strict app/` / `npm run typecheck`).
- [ ] All unit and security tests pass locally (`pytest tests/unit tests/security/injection_harness`).
- [ ] New functionality includes corresponding unit tests.
- [ ] No hardcoded credentials, secret tokens, or absolute local paths introduced.
- [ ] For changes to `orchestrator/` or `mcp_server/`: verified against the security threat model (PRD §20).
