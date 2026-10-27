# Test report

Latest report: **2026-10-23**. This report describes the tests that exist, what they
cover, and what was actually run in this environment. It does not claim live X or live AI
verification.

## Environment

| Item | Value |
| --- | --- |
| OS | Linux 6.17.0-PRoot-Distro aarch64 (Termux/PRoot) |
| Python | 3.14.4 |
| pytest | 9.1.1 |
| Node.js | v22.23.3 |
| ruff | 0.17.0 |
| mypy | 2.4.0 |
| Required Python dependency | PyYAML available |
| Optional tools | `requests` and PIL available; `twscrape`, `Scweet`, `pytesseract` unavailable; `x-cli` binary absent |

The suite runs fully with the deterministic no-AI provider and synthetic fixtures. No
test calls X, FxTwitter, Nitter, an AI endpoint or another live network service.

## Summary

| Gate | Command | Result |
| --- | --- | --- |
| Formatting | `ruff format --check .` | 56 files already formatted |
| Lint | `ruff check .` | All checks passed |
| Types | `mypy xvote` | Success: no issues found in 29 source files |
| Bytecode compile | `python3 -m py_compile $(find xvote -name '*.py')` | Passed |
| Frontend syntax | `node --check web/app.js` | Passed |
| Pytest | `pytest` | **92 passed in 6.88s** |
| HTTP smoke | `python3 /tmp/opencode/xvote_smoke.py` | **66 checks passed, FAILURES: none** |
| Dependency audit | `pip-audit --no-deps -s osv -r /tmp/opencode/xvote-requirements.txt` | No known vulnerabilities for the pinned PyYAML runtime dependency |

`pytest` is configured with `-q`; use `pytest -q` or `pytest` from the repository root.
In this environment `python3 -m pytest` does not find the pytest installation; the
`pytest` executable is the supported command.

## Test inventory (92 tests)

| Suite | Tests | Focus |
| --- | --- | --- |
| `tests/test_adapters.py` | 9 | Capability probes, router fallback and partial results, manual/OCR parsing, missing credentials, timeouts, no fabricated data, synthetic gating |
| `tests/test_agents.py` | 5 | Deterministic orchestration, bounded depth, unchanged-session skip, schema validation, AI suggestions cannot mutate counts |
| `tests/test_config.py` | 8 | Config defaults/validation, adapter enablement, secret handling, competition loading |
| `tests/test_e2e_demo.py` | 1 | Demo session end to end: collection, rankings, unresolved counts, approval, snapshots, CSV export, deletion while export survives |
| `tests/test_eligibility.py` | 12 | Window, project matching, support statements, ambiguity, account/method constraints, supersession, stable event IDs, self-supersede guard, edit/deletion policy |
| `tests/test_export.py` | 5 | CSV/JSON/HTML, field selection, sensitive-field gate, CSV injection neutralization, synthetic-data labels and source context |
| `tests/test_interpretation.py` | 4 | Natural-language rule interpretation, ambiguity blocking, unsupported live source acknowledgement |
| `tests/test_ranking.py` | 11 | All ranking views, zero votes, half-up rounding, ties, denominators, aggregate separation, timeline, custom formula |
| `tests/test_server.py` | 4 | HTTP security headers, CSRF/content-type enforcement, public template endpoint, route behavior |
| `tests/test_service.py` | 7 | Session lifecycle, result status/finalization, live compliance gate, overview, review, export persistence, checkpoint and cleanup |
| `tests/test_storage.py` | 7 | Memory/SQLite behavior, checkpoint restore, snapshot ordering, retention |
| `tests/test_templates.py` | 2 | Template IDs/formats validity, aggregate templates remain separate, no credential strings |
| `tests/test_textmatch.py` | 4 | Alias/phrase boundaries, hashtags, ambiguity and conflict handling |
| `tests/test_urls.py` | 13 | X URL parsing, lookalike rejection, allow-lists, SSRF-relevant validation |

No line/branch coverage percentage is claimed; no coverage tool was installed or run.
Test counts are from `pytest --collect-only`.

## What the E2E test asserts

`tests/test_e2e_demo.py` creates the labelled synthetic demo session and checks:

- 282 observed activity records with the expected confirmed/provisional/pending/
  ambiguous/rejected split and a real superseded duplicate;
- deterministic scores Alpha 121, Beta 94, Gamma 47;
- aggregate votes remain separate and are not presented as individual voters;
- pending review approval updates the event and the ranking without confirmation by
  default;
- all leaderboard views, snapshots and comparison work;
- CSV export is written and remains after session deletion;
- unresolved and unassigned observations are reported rather than dropped.

## HTTP smoke coverage (66 checks)

The smoke script starts the real server on loopback with isolated session/export roots and
verifies:

- health, settings, templates and adapter status;
- CSRF and content-type rejection;
- demo creation, synthetic banners, overview counts and limitations;
- confirmed/provisional/raw/method/account/weighted/timeline views;
- score, denominator, source and unresolved metadata, including unassigned observations;
- event detail/review, bulk-review confirmation and poll review;
- coverage, snapshots and snapshot comparison;
- export fields, CSV creation and download, and sensitive-field refusal;
- interpretation endpoint and static asset serving;
- static traversal rejection;
- live collection blocked without the authorized-use acknowledgement;
- session cleanup confirmation, client-header requirement, deletion and confirmed removal.

The smoke run creates temporary fixtures and deletes its sessions. It does not exercise
live adapters or live X content.

## Reproduce

```bash
cd /data/data/com.termux/files/home/x-vote
ruff format --check .
ruff check .
mypy xvote
python3 -m py_compile $(find xvote -name '*.py')
node --check web/app.js
pytest
printf 'PyYAML==%s\n' "$(python3 -c 'import yaml; print(yaml.__version__)')" > /tmp/opencode/xvote-requirements.txt
pip-audit --no-deps -s osv -r /tmp/opencode/xvote-requirements.txt
```

For the HTTP smoke test, start an isolated server (see the current run notes) and execute:

```bash
python3 /tmp/opencode/xvote_smoke.py
```

The smoke script is an external verification artifact and is not part of the shipped
repository.

## Gaps and residual risk

- No live FxTwitter/Nitter/twscrape/x-cli/Scweet request was made; timeout, auth, parser
  and fallback paths are tested with fakes only.
- Image OCR was not exercised because `pytesseract`/`tesseract` are absent; the text OCR
  path is tested.
- No live AI provider call was made. Tests assert suggestions are inert and schema-checked.
- UI behavior is covered through the API/static server and JavaScript syntax check, not a
  real-browser automation suite.
- No load, soak, fuzz or coverage-percentage measurement was performed.
- Tests use synthetic data and do not establish legal, identity or election validity.

See [LIMITATIONS.md](LIMITATIONS.md) for product-level limits and
[SECURITY.md](SECURITY.md) for the threat model.
