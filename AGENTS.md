## Core Requirements

- Must communicate in English

## Project Overview

- Xcash: an open-source, enterprise-grade cryptocurrency payment gateway built on Django, supporting payments and deposits, with multi-merchant and multi-project management.
- Use `uv` for environment management; run Python commands locally with `uv run`.
- Address model private keys only exist inside the internal system; sending transactions from outside the system is impossible, so EVM nonces must strictly increment from 0 on each chain.
- The admin theme uses django-unfold; admin page development must follow a consistent UI style.
- Locally, use docker-compose.dev.yml to spin up the database, redis, and test-chain services.
- Generated docs files are never committed to git.
- Write commit messages in English, and append the model information used for this development at the end.
- Develop on the main branch whenever possible.
- The project is compatible with the YiPay V1 API; docs: https://pay.v8jisu.cn/doc_old.html
- `xcash-saas` is the SaaS commercialization layer of the xcash crypto payment gateway; xcash is the blockchain engine.
- **xcash repo**: `/Users/void/PycharmProjects/xcash` (this project, Django 5.2)
- **xcash-saas repo**: `/Users/void/PycharmProjects/xcash-saas` (Django 5.2 + DRF)

## Testing Requirements

- Only write tests for behavioral correctness: core business logic, state transitions, concurrency safety, amount calculations, external API exception handling, etc.
- Do not write tests for pure configuration values, resource tiers, or display copy — such rules are governed by README or deployment docs. Only add behavior tests when config parsing, invalid-value validation, environment variable overrides, permission boundaries, or business behavior are affected.
- For renames that don't change business logic (config names, constant names, enum values, resource tier names), only update old names/values in existing tests; do not add new tests just to prove a new name exists or an old name is gone. This constraint takes priority over the general TDD flow.
- By default, no tests for pure presentation changes, including but not limited to: whether admin list columns display, field ordering, copy/display text, readonly_fields configuration, help_text, verbose_name — anything that doesn't affect business logic.
- Tests for an admin presentation config are only allowed if it directly affects business operation results, permission boundaries, or security — and the rationale must be stated explicitly.
- Avoid adding maintenance cost for low-value, brittle UI-config tests.

## Coding Requirements

- Feature design should follow first principles; avoid over-engineering.
- Core business logic needs complete logic comments; key code and logic must have corresponding unit tests.
- Never add comments in smart contracts (Solidity/TVM etc.): keep contract source lean and self-explanatory through clear naming; no piles of explanatory comments. For smart contracts this rule takes priority over the previous one ("core business logic needs complete logic comments").
- Keep code logic clear and human-readable.
- All external API calls (blockchain nodes etc.) must have exception handling and timeout settings.
- Method names must not start with `_`.
- All Django/DRF API endpoints in this project use URLs without trailing slashes: never define, generate, document, or request routes ending with `/`, and always keep APPEND_SLASH=False and DRF router trailing_slash=False.

## Migration Requirements

- **Any DB operation that narrows the valid data domain** (adding NOT NULL fields, altering existing fields to NOT NULL, adding UNIQUE / unique_together / CHECK / FK constraints, tightening field lengths or enum ranges, etc.) must, in the same migration or a preceding migration, normalize existing data via `RunPython` using deterministic, idempotent rules before applying the narrowing operation. Document the normalization rules in the RunPython function docstring, explaining how conflicting data is handled (backfill values / dedup criteria / orphan deletion, etc.) for post-mortem ops troubleshooting.
- Because `django-migration-linter` cannot see RunPython backfill/cleanup semantics, it flags these narrowing steps as `NOT_NULL` / `UNIQUE` violations: you must insert `IgnoreMigration()` (`from django_migration_linter.operations import IgnoreMigration`) in that migration file as a targeted exemption, with a comment above operations stating "safe because normalized via RunPython". Global exclusion rules or lowering linter levels are forbidden as workarounds.
- Because the previous rule requires `from django_migration_linter.operations import IgnoreMigration` at module top level in migration files, `django-migration-linter` must live in `[project].dependencies` in pyproject.toml (not the `dev` group), otherwise production images won't install it and `migrate` fails loading migration modules with ModuleNotFoundError. The package only joins INSTALLED_APPS under dev settings; production installs but doesn't enable it — no runtime side effects.
- **If a constraint has no acceptable default normalization rule** (typically UNIQUE conflicts where the surviving row can't be determined mechanically, or FK orphans whose deletion loses business data), **adding the constraint directly in that migration is forbidden**. Instead, ship an intermediate migration that only flags/bypasses conflicting rows + logs warnings for manual ops handling, then add the constraint next release; or downgrade the constraint to application-layer validation, stopping new conflicts at the source while legacy data drains naturally.
- If a RunPython reverse function cannot truly restore the data, write a documented no-op explaining why, instead of leaving it empty or raising exceptions.
- Constraints within newly created tables (first migrations), and added columns with constant defaults, are exempt from these rules.

## Security Requirements

- Financial operations must prevent concurrent races (use select_for_update or other effective measures) and guarantee data safety and consistency.
- Sensitive logs must never record private-key-related information.
