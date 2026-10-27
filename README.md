# x-vote

x-vote is a local, privacy-conscious voting checker for X campaigns. It collects post,
reply, poll, like, repost, quote and keyword observations through **open-source or
operator-supplied acquisition paths**, verifies eligibility against explicit rules,
keeps unresolved evidence unresolved, and produces deterministic, auditable rankings
and exports.

It is not an official X product and does not use the X API. It does not bypass
authentication, CAPTCHA, access controls or rate limits. Only use it with authorized
access and in compliance with the applicable tool licenses, X's terms and self-hosted
instance policies.

## What it does

- Guided New Check wizard with built-in templates for every supported voting format,
  advanced collection controls and an explicit session-storage choice.
- Deterministic ranking engine with confirmed, provisional, raw, method, account,
  weighted and timeline views.
- Competition rulesets with a SHA-256 ruleset version, event re-evaluation without
  recollection, and manual-review audit records.
- Source post author and public engagement metrics retained as context with timestamps,
  clearly marked as never votes.
- Anonymous poll results kept as source-reported aggregates, never presented as
  individually verified voters.
- Mixed aggregate + individual checks kept separate by default; combining them requires
  an explicitly documented operator decision.
- Open-source acquisition adapters plus manual CSV/JSON, OCR-text/image and synthetic
  fixture paths, behind a capability-aware router.
- Bounded deterministic multi-agent review; AI is optional and can only record
  suggestions, never change counts or rules.
- Session-scoped temporary storage (memory with optional SQLite promotion), checkpoint
  recovery, restrictive permissions and honest cleanup reporting.
- Local web UI with responsive layouts, an advanced configuration editor, review queues,
  coverage reporting, snapshots, export controls and visible synthetic-data / limitation
  banners.
- CSV, JSON and HTML exports with field selection, sensitive-field confirmation and CSV
  formula-injection neutralization.

## Quick start

Requirements: Python 3.11+ and PyYAML. The application runs fully without AI.

```bash
cd x-vote
python3 -m pip install -e .
python3 -m xvote demo          # synthetic end-to-end demo; no live X access
python3 -m xvote serve         # http://127.0.0.1:8765
```

Open `http://127.0.0.1:8765`, create a check, collect or import evidence, review
unresolved events, then inspect the leaderboard and export.

The built-in demo is synthetic test data, clearly labelled and never mixed with live
results. Its deterministic scores are Alpha 121, Beta 94, Gamma 47. Overview counts are
`confirmed_votes=262` (7 individual + 255 aggregate), `provisional_votes=17`,
`pending_review=3`, `ambiguous=1`, `total_unresolved=4` and
`total_observed_activity=282`. One observation cannot be associated with a project and
is reported globally rather than silently dropped.

The CLI demo deletes its temporary session before exiting. A demo started in the UI
remains available for inspection until you delete the session or run cleanup:

```bash
python3 -m xvote sessions list
python3 -m xvote cleanup
```

Exports are intentionally outside session cleanup and remain on disk until removed by
the operator.

## Live acquisition

Live collection is disabled until the operator acknowledges authorized use:

```bash
cp .env.example .env && chmod 600 .env   # only if .env does not already exist
# Set XVOTE_AUTHORIZED_USE_ACK=yes inside .env only after confirming authorization/terms.
```

Then configure an available adapter in `config/acquisition.yaml`, provide credentials
through environment variables only, and add sources to a competition. Manual imports and
synthetic demos do not require the acknowledgement. See
[docs/INSTALL.md](docs/INSTALL.md) and [docs/ADAPTERS.md](docs/ADAPTERS.md).

> The open-source live adapters are implemented but could not be exercised against X in
> this build environment because `twscrape`, `Scweet` and `x-cli` were not installed.
> Their probes, fallback behavior, capability reporting and failure paths are tested;
> live network behavior is not claimed as verified. See
> [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Documentation

| Document | Contents |
| --- | --- |
| [docs/INSTALL.md](docs/INSTALL.md) | Installation, Nitter, twscrape, x-cli, Scweet, OCR and AI setup |
| [docs/USAGE.md](docs/USAGE.md) | UI and CLI workflow, rules, review, views, exports, cleanup |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Modules, data flow, storage, ranking and determinism |
| [docs/SECURITY.md](docs/SECURITY.md) | Controls, threat model, secrets, SSRF, exports, residual risks |
| [docs/ADAPTERS.md](docs/ADAPTERS.md) | Capability and permission matrix, setup status and fallback rules |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | What is implemented, tested, unverified or explicitly unsupported |
| [docs/TEST_REPORT.md](docs/TEST_REPORT.md) | Test inventory, commands and latest results |
| [docs/IMPLEMENTATION_SUMMARY.md](docs/IMPLEMENTATION_SUMMARY.md) | Completed, partial, unverified and unsupported feature summary |

## Tests and quality gates

```bash
pytest                 # unit, integration and HTTP E2E tests
ruff check .
mypy xvote
python3 -m py_compile $(find xvote -name '*.py')
node --check web/app.js
```

The suite has 92 tests. The synthetic HTTP smoke test described in
[docs/TEST_REPORT.md](docs/TEST_REPORT.md) exercises the demo, templates, all leaderboard
views, review, snapshots, exports, compliance gate, static-file safety and cleanup.

## Repository layout

```text
xvote/               Python application service, ranking, adapters, agents, storage
web/                 Vanilla HTML/CSS/JS single-page UI served by the app
config/              Acquisition configuration (no credentials)
tests/               Pytest unit, integration and E2E suites
docs/                Architecture, security, usage, adapter and test documentation
PLAN.md              Implementation plan and stack decisions
.env.example         Environment variable template (credentials only via env)
```

## Security summary

The server binds to loopback by default, rejects cross-origin-style mutations without a
custom request header, applies CSP and framing protections, caps request bodies, blocks
static traversal and confines exports. Credentials are read only from environment
variables and never serialized. Account-level and raw-evidence exports require explicit
confirmation. These controls reduce risk but do not replace host hardening; never expose
the server directly to the public internet. See [docs/SECURITY.md](docs/SECURITY.md).

## Known limitations

x-vote cannot verify that an observed account is a unique human, cannot observe
activities outside configured sources, cannot make an anonymous poll individual, and
cannot guarantee completeness where an adapter, source or method is unavailable. Live
adapter behavior must be validated by the operator in their own authorized environment.
See [docs/LIMITATIONS.md](docs/LIMITATIONS.md) for the full list.
