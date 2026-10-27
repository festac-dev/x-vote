# Architecture

## Overview

x-vote is a single-process Python application with a standard-library HTTP server and a
vanilla JavaScript frontend. It deliberately avoids a build pipeline, a cloud database and
mandatory third-party services.

```text
Browser (web/)
   │ JSON over loopback, custom request header on mutations
   ▼
ThreadingHTTPServer (xvote/server.py)
   │ routes + validation + security headers
   ▼
AppService (xvote/service.py)
   ├── SessionManager / EventStore (xvote/storage.py)
   ├── Competition + rules (xvote/models.py)
   ├── AcquisitionRouter → adapters (xvote/adapters/)
   ├── normalization + eligibility + dedupe (xvote/normalization.py, eligibility.py)
   ├── ranking + snapshots + timeline (xvote/ranking.py)
   ├── agents (xvote/agents/)
   └── exports (xvote/export.py)
```

## Module map

| Module | Responsibility |
| --- | --- |
| `models.py` | Dataclasses and status enums; event/poll/audit/snapshot models; ruleset hashing |
| `urls.py` | X URL/post ID parsing, outbound URL allow-listing, bounded fetches, no redirects |
| `textmatch.py` | Word-boundary alias/keyword matching, intent suggestions |
| `normalization.py` | Adapter payload → normalized events/polls; dedupe keys; provenance |
| `eligibility.py` | Window, association, method, account, evidence and edit/deletion rules; manual review |
| `ranking.py` | All leaderboard views, scores, shares, limitations, snapshots, timeline |
| `fixtures.py` | Synthetic demo fixture, visibly labelled test data |
| `interpretation.py` | Deterministic plain-language configuration draft |
| `config.py` | YAML loader, env-only secrets, AI/compliance configuration |
| `storage.py` | Session directories, memory/SQLite stores, checkpoint/restore, cleanup |
| `export.py` | Record projection, CSV/JSON/HTML generation, safe cells, field policy |
| `service.py` | Orchestration of every workflow and audit event |
| `server.py` | HTTP routing, request limits, CSRF-style header check, static serving |
| `cli.py` | `serve`, `demo`, `interpret`, `sessions`, `cleanup` |
| `adapters/base.py` | `AcquisitionAdapter` ABC and typed adapter errors |
| `adapters/router.py` | Ordered capability-aware fallback, attempt records, status mapping |
| `adapters/xtweetfetcher.py` | FxTwitter single post + self-hosted Nitter replies |
| `adapters/twscrape_adapter.py` | twscrape poll/replies/likers/reposters |
| `adapters/xcli.py` | Fixed read-only x-cli JSONL commands |
| `adapters/scweet_adapter.py` | Opt-in Scweet replies/reposters |
| `adapters/manual.py` | CSV/JSON import, OCR text/image evidence validation |
| `adapters/synthetic.py` | Fixture-only adapter for tests and demo |
| `agents/providers.py` | No-AI, local/remote OpenAI-compatible provider abstraction |
| `agents/runtime.py` | Bounded task plan, schema validation, retries, dedupe, role workers |

## Data model

- **Session** owns temporary paths, an `EventStore`, competitions, coverage, agent tasks,
  exports and checkpoint state.
- **Competition** contains projects, sources, methods, eligibility/evidence/ranking/
  collection rules, status and a ruleset version.
- **Project** has a name, optional external ID, aliases, hashtags, usernames and keywords.
- **Source** identifies a post/query and the methods it supports.
- **NormalizedEvent** records method, source, account (when observed), timestamp,
  normalized value, provenance, dedupe key, eligibility/verification/review status,
  confidence, decision reason, ruleset version and supersession link.
- **PollAggregate** records an option label, integer votes, source provenance, exactness
  and review state. It never contains synthetic individual voters.
- **Snapshot** stores rank/count maps, ruleset and coverage at a point in time.
- **AuditRecord** captures actor, action, before/after state and note.

Compatibility helpers (`from_dict`, `to_dict`) validate types and lengths at ingestion.

## Workflow

### Competition creation

The operator submits a fully formed configuration with `confirmed=true`. The service
validates projects, sources, methods and rules, computes a ruleset version, creates
coverage placeholders and writes an audit record. The interpretation endpoint is only a
draft; it does not create or activate a competition.

### Collection

1. `AppService.collect` checks finalization and the live-source compliance gate.
2. The router orders adapters by operator preference, then skips disabled/unavailable/
   unsupported adapters.
3. Each candidate is called with a timeout. Failures are recorded as typed attempts and
   the next permitted adapter may be tried.
4. Returned observations are normalized, deduplicated and associated with projects.
5. Eligibility evaluates each event against the current ruleset. Unresolved items stay
   unresolved; nothing is silently confirmed.
6. Coverage is updated with per-source status and unavailable methods.
7. A snapshot is created if enabled, and deterministic agents can run over the result.

An empty list is not proof that no replies exist. The coverage status and limitations
carry that distinction.

### Manual aggregate and OCR imports

Manual CSV/JSON records are validated field by field. Aggregate records require review by
default. OCR text is parsed as `Project: N votes` lines; OCR images are validated by magic
bytes, size and image verification before text extraction. Every OCR result is labelled
low-confidence/manually supplied and remains distinguishable from individual events.

### Review

`apply_manual_review` performs approve/reject/reassign/inconclusive/dispute transitions,
records before/after state, and prevents unsupported transitions. Bulk review requires
explicit confirmation. The service then re-applies account constraints and checkpoints.

### Ruleset changes

`update_competition` rebuilds the competition, validates it, computes a new ruleset
version and re-evaluates stored events/polls without re-collecting. Events with a manual
decision retain that decision but move to the new ruleset version.

## Ranking semantics

For each project, the engine aggregates active (non-superseded) events and poll
aggregates:

- Confirmed individual counts, confirmed aggregate counts, provisional counts, rejected/
  late activity, raw activity, per-method breakdowns, confidence breakdown.
- Pending and ambiguous counts; `unresolved_count = pending_review + ambiguous`.
- Distinct observed account IDs among confirmed individual events.
- Coverage and limitations inherited from the source/competition.

Mixed aggregate + individual competitions are **separate by default**:

- Score = confirmed individual events only.
- Aggregate totals are shown separately.
- Combination requires an explicit `combine_confirmed_with_aggregates` setting, a
  weighted mode or a documented custom formula.

Other modes: `separate`, `weighted`, `custom` (AST-whitelisted formula evaluator) and
`unique_account`. The `weighted` view recomputes scores from per-method counts using
operator weights and labels the result “not a vote count”. Custom formulas cannot call
arbitrary Python or access files/network.

Ties are competition-style (`1, 1, 3`), sorted by score descending, then case-folded name,
then project ID. Vote shares are percentages of the selected metric's total and are
`null` when that total is zero. Zero-vote projects remain listed.

Unassigned unresolved observations are counted in the leaderboard’s global
`unassigned_events`/`unassigned_pending_review` fields and in `total_unresolved`; they are
not silently attributed to a project.

Every leaderboard exposes metric, metric description, score label, denominator kind,
coverage status, finalization status, formula, sources and limitations, plus a
`is_test_data` flag.

## Determinism boundary

Given the same approved events, poll aggregates, competition configuration and ruleset:

- event IDs and dedupe keys are stable hashes;
- eligibility, dedupe, score, rank, share and snapshot rank/count maps are deterministic;
- no AI or network result participates in counts;
- timestamps such as `generated_at` vary, but do not participate in ordering or scoring.

Network collection itself is not deterministic; the tool records what it observed and
never fills gaps with invented data.

## Storage and lifecycle

- Session root defaults to a private temp directory (or `XVOTE_SESSION_ROOT`), mode 0700.
- Memory store by default; optional SQLite store/promotion at the configured threshold.
- Checkpoints are written atomically to `checkpoint.json` with mode 0600.
- `SessionManager.restore` recovers valid sessions, removes orphans and discards
  unreadable checkpoints with a note.
- Cleanup closes stores, removes session directories and reports per-session failures.
- Export files are deliberately outside the session directory and survive cleanup.

## Agents

The runtime builds a bounded plan (orchestrator plus deterministic roles such as alias
matching, classification, duplicate analysis, eligibility verification, evidence audit,
poll reconciliation, leaderboard verification, QA and report generation). Tasks are:

- schema-validated against a per-role allow-list;
- limited by concurrency, depth, timeout and retry settings;
- deduplicated by a signature over competition, input hash and role;
- recorded with status, confidence, limitations and error codes.

AI is optional. Model output is accepted only as a suggestion in event metadata and is
never allowed to alter eligibility, counts, rules, permissions or tool access.

## Frontend

`web/index.html`, `styles.css` and `app.js` implement a responsive single-page UI with:

- welcome, overview, competitions, new-check wizard, sources, events, leaderboard,
  agents, evidence review, exports and settings sections;
- tables on desktop and card-like layouts on narrow screens;
- explicit test-data, coverage, provisional and limitation messaging;
- no external fonts, CDNs or third-party scripts;
- text inserted through DOM text nodes or escaped template helpers.

## Extending

To add an adapter:

1. Subclass `AcquisitionAdapter`, implement the six operations, `capabilities()`,
   `available()`, `version_info()` and an honest `license_note`.
2. Add settings and construction in `AppService._build_router`.
3. Add it to `config/acquisition.yaml` and `.env.example` without credentials.
4. Add router/unit tests for capability skips, failures, empty results and normalization.
5. Document the permission/capability matrix in `docs/ADAPTERS.md`.

Do not add an acquisition path that bypasses authentication, CAPTCHA, access controls or
rate limits.
