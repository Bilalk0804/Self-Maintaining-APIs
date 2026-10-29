# Prefix convention (project rules)

Source: cursor-rules-principles.md and commands/retro.md.

## Prefix table (cursor-rules-principles)

| Prefiks | Plik | Zakres |
|---------|------|--------|
| 000 | 000-core.mdc | core – kontekst, workflow, dokumentacja, błędy |
| 100 | 100-environments.mdc | environments – środowiska, Docker, sekrety i konfiguracja, debug |
| 200 | 200-security.mdc | security – bezpieczeństwo, KeePass, rotacje |
| 300 | 300-deploy.mdc | deploy – CI/CD, GitHub Actions, Nginx |
| 400 | 400-versioning.mdc | versioning – VERSION, version.json, release |
| 500 | 500-api-tests.mdc | api-tests – specyfikacje API, testy, Swagger |

## Retro naming (commands/retro.md)

- `000-core.mdc` – alwaysApply: true; minimal "laws"
- `050-workflow.mdc` – how we work in THIS repo; review/test expectations
- `100-<lang>.mdc` – scoped by globs (e.g., Python/TS)
- `200-<domain>.mdc` – infra, data-import, security-hardening
- `900-ai-meta.mdc` – meta: how to use rules/skills in THIS repo; keep tiny

## General principles

- Rules in `.cursor/rules/` as `.mdc` with frontmatter (`description`, `alwaysApply`, optional `globs`).
- One responsibility per file, ~&lt;50 lines (create-rule best practice).
- Keep rules folder flat; no subfolders inside PROJECT_RULES_DIR.
