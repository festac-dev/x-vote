# Usage

## Start the application

```bash
python3 -m xvote serve                 # 127.0.0.1:8765
python3 -m xvote serve --port 9000     # loopback by default
```

Open the printed URL. The UI and API are served by the same Python process. Static files
come from `web/`; there is no separate frontend server.

For live collection, set the authorized-use acknowledgement only after you have confirmed
authorization and accepted the applicable terms:

```bash
export XVOTE_AUTHORIZED_USE_ACK=yes
```

Fixture and manual imports work without it. A live collection attempt without the
acknowledgement fails with HTTP 428 and code `authorized_use_not_acknowledged`; it does not
fall back to silently collecting through another path.

## CLI commands

```bash
python3 -m xvote demo [--no-collect]
python3 -m xvote interpret "Count votes for Alpha and Beta from https://x.com/a/status/123, include replies and likes, ends Friday at 8 PM UTC" --name "My check"
python3 -m xvote sessions list
python3 -m xvote sessions delete <session_id>
python3 -m xvote cleanup
```

`interpret` is a deterministic draft only. It never starts collection. Review every field
before confirming a competition.

## Typical workflow

1. **Start a check.** Choose auto/memory/SQLite session storage on the welcome screen,
   then open New Check. Choose a template to prefill a supported format (poll, reply, like, repost, quote, mention, hashtag, keyword,
   weighted, external aggregate, manual/CSV/JSON, OCR or custom). Templates supply
   defaults only; every field remains editable.
2. **Describe the check.** The New Check wizard accepts a plain-language instruction and
   produces a deterministic draft, or you can fill in the fields directly. The draft is
   not active until you explicitly create/confirm the competition.
3. **Review advanced settings.** The Ranking step also stores collection frequency,
   permitted collection duration and automatic snapshot preference. These are
   preferences: collection is still operator-started, and session storage is selected on
   the welcome screen and remains temporary.
4. **Define projects and sources.** Projects can use names, aliases, IDs, hashtags,
   usernames, keywords and domains. Sources specify post IDs/URLs, methods, an optional
   project association and fixture references where applicable. The Competitions page
   also has an advanced JSON editor for identifiers, per-source methods, weights and rules
   not exposed in the guided wizard; saving is confirmed and re-evaluates evidence.
5. **Collect or import.** Run the configured adapters, import a manual CSV/JSON total, or
   provide OCR text/image evidence. Manual JSON/CSV records may set `content_status` to
   `unchanged`, `edited` or `deleted`; coverage records what each source/method returned.
6. **Review evidence.** Vote Events shows every observation, including rejected,
   provisional and superseded records. Evidence Review exposes unresolved/ambiguous items.
7. **Rank and finalize.** Inspect the selected leaderboard view, then set the result
   status to collecting, provisional or finalized. Every view names the metric,
   denominator, sources, coverage, finalization status and limitations.
8. **Capture snapshots.** Capture a snapshot before and after changes; compare the latest
   two to see rank and count changes separately.
9. **Export.** Choose kind/format/fields. Account-level and raw-evidence fields require
   explicit confirmation. Exports are local files and are not deleted with the session.
10. **Clean up.** Delete the session or run `cleanup`; temporary session directories and
   SQLite files are removed. Exported files remain.

## Competition rules that matter

- **Window:** timestamps are normalized to UTC and compared against the inclusive
  `[starts_at, ends_at]` window. Naive operator timestamps receive the competition timezone.
- **Association:** names/aliases/IDs/hashtags/usernames must match with word boundaries.
  Multiple matching projects become an ambiguous association and remain unresolved until
  an operator decides.
- **Support statement:** reply/quote/mention methods require a support statement by
  default; a bare mention is not automatically a vote.
- **Per-account constraints:** one vote per account per method by default. Conflicting
  projects from the same account can become disputed.
- **Edits/deletions:** governed by eligibility settings; deleted content is not eligible by
  default. Manual overrides are audited.
- **Duplicates:** the same method/source record is stored once. When a higher-confidence
  observation supersedes another, the old record remains visible but is excluded from
  ranking.
- **Ruleset changes:** changing competition rules creates a new ruleset version and
  re-evaluates events without re-collecting. Manual decisions are preserved.
- **Poll data:** option labels map to projects, but vote counts stay aggregate. Poll totals
  are never expanded into individual voters. Inconsistent totals and unavailable
  methods/sources are surfaced.
- **Manual/OCR aggregates:** labelled as manually supplied and requiring review; they are
  never treated as individually verified.

## Leaderboard views

| View | Score shown | Important caveat |
| --- | --- | --- |
| Confirmed | Confirmed eligible votes under the ruleset | Aggregates are included only when the operator explicitly allowed combining; otherwise they are separate |
| Provisional | Unresolved activity pending review | Never confirmed and never final |
| Raw | Every observed activity item | Includes rejected and unresolved items; not valid votes |
| Method | Confirmed totals with per-method breakdown | Aggregates remain distinguishable from individual events |
| Account | Distinct observed account IDs in confirmed individual events | Accounts are not unique humans; anonymous poll votes are excluded |
| Weighted | Operator-weighted method combination | Not a vote count; original counts remain preserved |
| Timeline | Observation timeline and snapshot history | Reference view, not an alternate count |

Ties use competition ranking (for example `1, 1, 3`). Projects with zero votes still
appear. Vote share uses the total of the selected metric and is `null` when the
denominator is zero. In a mixed-method competition that has not explicitly enabled
combination, confirmed scores use confirmed individual events only and aggregate counts
are reported separately.

## Review actions

- **Approve** marks an event eligible and verified and can reassign a project.
- **Reject** marks it ineligible/rejected.
- **Reassign** moves it to another project and returns it to review.
- **Inconclusive** keeps it unresolved.
- **Dispute** marks it disputed, if the rules allow manual overrides.
- **Poll review** supports approve, reject and inconclusive; it never creates individual
  voter records from an aggregate.

Every manual action writes an audit record with the before/after state. Bulk review requires
`confirm=true` and is limited to 500 events per request.

## Exports

Available kinds include leaderboard, method, events, pending, rejected, coverage,
snapshots, audit and agents. Formats are CSV, JSON and HTML.

- Default fields exclude account IDs and raw evidence excerpts.
- Selecting sensitive fields requires `confirm_sensitive` (UI checkbox) and records that
  confirmation in the export metadata and audit log.
- CSV cells beginning with `=`, `+`, `-`, `@`, tab or carriage return are prefixed with an
  apostrophe to prevent spreadsheet formula injection.
- HTML exports escape content and include the synthetic-data banner and limitations.
- JSON exports include a `test_data` flag, an explicit notice and the same limitations.
- Exports are written with mode 0600 under the export directory and never uploaded.

## API

The UI uses the local JSON API under `/api`. Mutating methods (`POST`, `PUT`, `DELETE`)
require the header:

```text
X-Requested-With: x-vote
```

They also require `Content-Type: application/json`. A missing custom header returns 403
`missing_client_header`; a wrong content type returns 415. Sessions are addressed by
`/api/sessions/{session_id}`; competition endpoints include
`/api/sessions/{sid}/competitions/{cid}/...`.

Core routes:

```text
GET  /api/health
GET  /api/settings
GET  /api/adapters
POST /api/sessions
POST /api/demo
GET  /api/sessions/{sid}/overview
POST /api/sessions/{sid}/competitions
POST /api/sessions/{sid}/competitions/{cid}/collect
POST /api/sessions/{sid}/competitions/{cid}/import
POST /api/sessions/{sid}/competitions/{cid}/ocr-text
POST /api/sessions/{sid}/competitions/{cid}/ocr-image
GET  /api/sessions/{sid}/competitions/{cid}/leaderboard?view=confirmed
POST /api/sessions/{sid}/competitions/{cid}/events/{event_id}/review
POST /api/sessions/{sid}/competitions/{cid}/bulk-review
GET  /api/sessions/{sid}/competitions/{cid}/polls
POST /api/sessions/{sid}/competitions/{cid}/polls/{aggregate_id}/review
POST /api/sessions/{sid}/competitions/{cid}/status
POST /api/sessions/{sid}/competitions/{cid}/snapshots
GET  /api/sessions/{sid}/competitions/{cid}/snapshots
POST /api/sessions/{sid}/competitions/{cid}/snapshots/{snapshot_id}/compare
GET  /api/sessions/{sid}/coverage
POST /api/sessions/{sid}/competitions/{cid}/export
GET  /api/sessions/{sid}/exports/{filename}
DELETE /api/sessions/{sid}
POST /api/cleanup
```

Exact request shapes are visible in `web/app.js` and `xvote/server.py`.

## Session storage and recovery

- Default root: a private subdirectory of the system temp directory, or
  `XVOTE_SESSION_ROOT`.
- Session directory mode 0700; checkpoints and evidence mode 0600.
- Memory storage by default; optional SQLite promotion when the configured size threshold
  is reached. SQLite files are session-scoped.
- On startup, `serve` restores valid checkpoints. Orphan or unreadable session directories
  are removed and reported in the log.
- Cleanup deletes temporary session artifacts and reports failures honestly. Exports live
  outside cleanup.
- Forced termination, power loss or a device failure can prevent the normal shutdown
  cleanup. On the next start, valid checkpoints are restored and orphan or unreadable
  session directories are removed. To inspect or remove a stale session manually, stop the
  server and run `python3 -m xvote sessions list`, then
  `python3 -m xvote sessions delete <session_id>`. As a last resort, `python3 -m xvote
  cleanup` removes all temporary sessions. Deleting a directory is not a claim of secure
  erasure on every filesystem.

## Common failure messages

| Code | Meaning |
| --- | --- |
| `missing_client_header` | Add `X-Requested-With: x-vote` to a mutating request |
| `confirmation_required` | Competition creation, bulk review or cleanup requires explicit confirmation |
| `authorized_use_not_acknowledged` | Live collection attempted without the compliance acknowledgement |
| `adapter_unavailable` / `credentials_missing` | The preferred adapter is not installed, enabled or configured |
| `rate_limited` | The source reported a rate limit; fallback may be attempted |
| `permission_restricted` | The source refused the operation; no bypass is attempted |
| `source_unavailable` | The source or tool could not be reached |
| `invalid_import` | Manual/OCR input failed validation |
| `invalid_export` | Unknown kind/format or sensitive fields without confirmation |

Unresolved and partial states are data, not errors: they remain visible in coverage,
event statuses and leaderboard limitations.
