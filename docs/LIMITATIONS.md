# Limitations and honest claims

x-vote produces **evidence-backed counts and rankings under disclosed rules**, not ground
truth about public opinion or voter identity. This document separates what is implemented,
what was tested in this environment, what depends on external tools, and what is
explicitly unsupported.

## Scope

- Local, single-operator tool. The HTTP server has no user accounts, roles, TLS or
  multi-tenant isolation.
- Open-source or operator-supplied acquisition only. x-vote does not use the official X
  API, does not bypass authentication/CAPTCHAs/access controls/rate limits, and does not
  guarantee compliance with X's terms or local law. The operator is responsible for
  authorization and lawful use.
- No payouts, airdrops, wallets, financial handling or automated enforcement.
- No scheduled monitoring or background collector. Collection starts when the operator
  starts it; a missing run is missing coverage, not proof of no activity.

## Acquisition and live coverage

| Area | Status |
| --- | --- |
| Synthetic fixture / demo | Implemented and exercised end to end |
| Manual CSV/JSON import | Implemented and tested |
| OCR text and image import | Text tested; image path requires Pillow/pytesseract/tesseract, which were not installed here |
| `x-tweet-fetcher` (FxTwitter + self-hosted Nitter) | Implemented; **no live network test** in this environment |
| `twscrape` | Adapter and routing tested; package/account pool unavailable, **no live test** |
| `x-cli` | Adapter and routing tested; binary/session unavailable, **no live test** |
| `Scweet` | Disabled by default and not installed; **no live test** |
| Optional AI provider | No-AI default tested; no live model endpoint was called |

Consequences:

- An empty result is not evidence that no votes exist. It can mean an unavailable adapter,
  rate limiting, a deleted/protected post, an unsupported method or a parsing change.
- Replies through Nitter depend on a self-hosted instance that is reachable, correctly
  configured and permitted. Public Nitter instances are not used.
- Live HTML/API formats can change without notice. Parsers are defensive, but no live
  compatibility claim is made.
- Only configured posts, methods, accounts and time windows are observed. Coverage is
  scoped and recorded per source; it is not a census of X.
- Deleted, edited, protected or late-arriving content may be unavailable or observed at a
  different time. Observation time is not the event time when the source does not expose
  the original timestamp.
- Edit/deletion eligibility policies apply when a record declares `content_status` (for
  example a manual import). The live adapters do not observe later edits or deletions, so
  absence from a later collection is not by itself proof of deletion.

## Identity, intent and duplicates

- An X account is **not** a unique human. The engine reports account-level evidence; it
  cannot prove personhood, control of an account, uniqueness across accounts or absence of
  coordinated behavior.
- An explicit support statement is required for reply/quote votes, but text matching is
  deterministic pattern logic, not a legal or semantic determination of intent.
- Ambiguous project mentions remain unresolved until a human reviews them. Rules do not
  guess between projects.
- A technical duplicate is the same method and reply/account observation key seen more
  than once. Similar text from different accounts is not merged.
- One account supporting multiple projects inside the window can be marked disputed by
  the account constraint. This is a policy outcome, not proof of fraud.
- Likes, reposts, quotes, mentions, hashtags and keywords are engagement signals. They are
  counted only when the ruleset explicitly enables them and they still identify an
  account; engagement counts are never silently treated as unique voters.
- Source-post likes, reposts, replies, quotes, views and bookmarks are stored only when
  the adapter exposes them, with collection timestamps and coverage notes. They are
  context for a result, never votes, and missing metrics are not assumed to be zero.
- Anonymous poll aggregates are source-reported totals. They are never expanded into
  individual voters, account counts or human verification. Poll option mapping is
  operator-configured and can be wrong if labels are ambiguous.

## Text, matching and OCR

- Alias/keyword matching uses word boundaries and conservative normalization. It can miss
  languages, slang, typo variants, obfuscated names, long context and quoted text, or match
  a project name that is also an ordinary word.
- URLs, hashtags, mentions, quotes and OCR noise can create false positives or missed
  matches. Review queues exist for this reason.
- Image OCR depends on external packages and image quality; OCR output is evidence text,
  not a verified ballot. The image path was not exercised here because OCR dependencies
  were absent.
- Optional AI suggestions are schema-validated and recorded for review only. They cannot
  change counts, eligibility or rules, and their accuracy is not guaranteed.

## Ranking semantics

- Rankings are deterministic for the same events, competition rules and ruleset version.
  Changing the ruleset, review decisions or source coverage can change results.
- Confirmed individual votes, provisional votes, raw observations, anonymous aggregates and
  weighted scores are different measures. A higher weighted score is not a larger number
  of voters.
- Poll aggregates are ranked separately by default. Combining aggregates with individual
  votes requires an explicit operator decision and is labelled accordingly.
- Ties fall back to configured project order/name. A rank is relative to the configured
  candidate set, not to all of X.
- Vote share uses the displayed denominator; zero-denominator views return no percentage.
  Coverage gaps and unresolved events are shown but excluded from confirmed scores.
- Exports preserve the selected field list. Removing denominator, coverage, ruleset or
  limitation fields makes an export less self-explanatory; it does not change the
  underlying session data.
- CSV/JSON/HTML leaderboard exports label synthetic data and include source/limitation
  context by default. Exports intentionally survive session cleanup and may contain
  account IDs, post excerpts or evidence references; protect them accordingly.

## Operations, storage and scale

- Session state is held in memory with an optional SQLite checkpoint. There is no
  replication, high availability or multi-process coordination. A crash can lose work
  since the last checkpoint; use the documented checkpoint/restore flow.
- Snapshots and sessions are bounded by configuration. Very large collections may hit
  memory, request-size, export-size or timeout limits.
- Session directories and checkpoints use restrictive permissions, but the same OS user
  can read them. There is no encryption at rest.
- The server binds to loopback by default and has no TLS or authentication. Do not expose
  it to a network. Reverse proxying without additional authentication is not supported.
- Cleanup reports failures honestly. It cannot remove files already copied elsewhere,
  open by another process, or retained in export directories.
- No backup, retention, data-residency or compliance certification is provided.

## Security residuals

See [SECURITY.md](SECURITY.md) for the full model. In short:

- Post/reply/OCR content is untrusted input and can contain prompt-injection attempts.
- Optional AI calls send the selected evidence excerpt to the configured endpoint; do not
  enable AI if that data cannot leave the host.
- Credentials are read from environment variables, but they remain visible to the same OS
  user/process environment. Use a dedicated account and least-privilege token.
- Rate limiting and CSRF-style header checks reduce accidental or cross-origin requests;
  they do not protect against a hostile local process or a compromised browser session.
- The tool does not provide content moderation, defamation review or legal advice.

## Explicitly unsupported

- Official X API integration or privileged data.
- CAPTCHA solving, login automation, rate-limit evasion, scraping of protected accounts,
  or any access-control bypass.
- Proof that an account is a unique human; bot/sybil detection claims.
- Anonymous poll voter de-anonymization or individual-level poll inference.
- Automatic finality for ambiguous/conflicting evidence without human review.
- Payouts, token distribution, wallets or financial settlement.
- Hosted multi-user service, public internet exposure, SSO, RBAC or audit-grade WORM
  storage.
- Continuous monitoring, alerting, scheduling or guaranteed freshness.
- Legal certification that a result is valid for a jurisdiction, contract or election.

## What would increase confidence

1. Validate each live adapter against an authorized test post and record the observed
   coverage in the session.
2. Review every pending/ambiguous event and export the audit trail.
3. Compare source coverage before interpreting a ranking, especially missing poll or
   reposter capabilities.
4. Keep the ruleset version, generated timestamp and limitations attached to every export.
5. Treat results as one evidence source among others, not as a substitute for identity
   verification or independent auditing.
