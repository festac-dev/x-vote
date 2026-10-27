# x-vote — Implementation Plan

Status: executed phase by phase. The repository contained only `prompt.txt`; this is a greenfield build.

## Stack decisions

- Python 3.14 standard library service (`ThreadingHTTPServer`), no mandatory framework, no cloud dependency.
- Vanilla HTML/CSS/JS frontend served by the same loopback-bound service (no build step, no CDN).
- PyYAML for `config/acquisition.yaml`; PyYAML already present. Everything else is stdlib.
- Optional inactive integrations are imported lazily: twscrape, Scweet, x-cli, pytesseract/PIL.
- Tests use pytest and synthetic fixtures; fixtures are gated behind an explicit test/demo flag and visibly labelled.

## Phase 1 — Foundation

- `xvote/models.py`: competitions, projects, sources, rules, normalized events, poll aggregates, snapshots, audit records.
- Deterministic event IDs and ruleset SHA-256 versioning.
- Safe X URL/ID parsing, timestamp/timezone normalization.
- Alias/keyword matching with word boundaries, conflict detection.
- Session storage: restrictive temp directory, in-memory repository, optional temp SQLite checkpoint, cleanup manager.
- Config loader and secret handling (env only, never serialized).

## Phase 2 — Acquisition

- `AcquisitionAdapter` ABC exactly covering tweet/replies/poll/likers/reposters/capabilities.
- Adapters:
  - XTweetFetcherAdapter: FxTwitter for single posts, configured self-hosted Nitter for replies.
  - TwscrapeAdapter: poll card options, replies, favoriters, retweeters.
  - XCliAdapter: fixed read-only commands with `-o jsonl`.
  - ScweetAdapter: replies/reposters, no poll.
  - ManualImportAdapter (CSV/JSON), OCREvidenceAdapter, SyntheticFixtureAdapter (explicitly gated).
- `AcquisitionRouter`: preference order, capability/credential checks, fallback, status recording, no fabrication.
- `config/acquisition.yaml` + `.env.example`; absent credentials disable adapters without crashing.

## Phase 3 — Verification, ranking, export

- Eligibility engine: window, association, methods, required patterns, per-account/method constraints, edits/deletions, ambiguity.
- Duplicate detection and highest-confidence adapter supersession.
- Verification statuses, manual review with audit trail, no silent confirmation.
- Ranking: confirmed/provisional/raw/method/account/weighted/timeline, competition ties, zero-vote entries, denominators, snapshots.
- Safe custom-formula evaluator (AST whitelist, no `eval`).
- CSV/JSON/HTML exports with field selection and sensitive-field confirmation.

## Phase 4 — Agents

- Bounded orchestrator → workers, max depth 3, concurrency/timeout/retry controls, duplicate suppression, typed messages.
- Deterministic workers: discovery, classification, alias, duplicates, eligibility aid, evidence audit, ranking verification, QA, report.
- Optional no-AI/local/remote provider abstraction; AI suggestions never change counts or rules.
- Source text always treated as untrusted data.

## Phase 5 — UI

- Overview, New Check wizard (templates + interpretation review), Sources coverage, Vote Events review, Leaderboard views, Agent Monitor, Exports, Settings.
- Responsive desktop tables/mobile cards, restrained data-analysis styling, explicit test-data and limitation banners.
- Hide irrelevant sections until a session/competition exists.

## Phase 6 — Hardening and delivery

- SSRF allow-list, no redirects, response caps, upload validation, no shell interpolation, redaction, loopback-only default bind.
- Cleanup with honest failure reporting; stale-session cleanup CLI.
- Docs: install (Nitter/twscrape/x-cli/Scweet), usage, architecture, security, adapter matrix, known limitations.
- Unit/integration/security/E2E tests; lint (`ruff`), typecheck (`mypy`), test report and implementation summary.

## Acceptance mapping

- Deterministic rankings from identical inputs/ruleset: ranking tests + E2E.
- Full function without AI: no-AI provider is the default in all tests.
- Non-developer flow: UI wizard → collection → review → ranking → export → cleanup.
- Anonymous poll aggregates never become individual voters; missing coverage is surfaced.

## Completion status

Status: implementation, documentation and verification complete for the delivered
scope. Live acquisition remains unverified by design in this environment.

Delivered:

- full service/API/UI, storage/checkpointing, acquisition adapters and router, agents,
  ranking, review, snapshots, exports and cleanup;
- synthetic demo with visibly labelled test data and deterministic expected results;
- docs for installation, usage, architecture, security, adapters, limitations and tests;
- 92 passing tests plus a 66-check HTTP smoke run; `ruff`, `mypy`, bytecode compile and
  frontend syntax checks pass.

Verification results and environment details are recorded in
[docs/TEST_REPORT.md](docs/TEST_REPORT.md). Live adapters, OCR image dependencies and AI
calls were not exercised against real services; see
[docs/LIMITATIONS.md](docs/LIMITATIONS.md).
