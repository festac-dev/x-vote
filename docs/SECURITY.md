# Security

x-vote handles credentials, account identifiers, post text, evidence references and
operator decisions. It is designed as a **local single-operator tool**, not a public
multi-tenant service. This document describes the controls that are implemented and the
risks that remain.

## Assets

- Adapter credentials: twscrape account pool, Scweet token, x-cli session cookie, optional
  AI key.
- Session data: normalized events, account identifiers, post text, evidence references,
  review notes, coverage and snapshots.
- Export files, which may intentionally include account-level or raw evidence fields.
- Ranking integrity: eligibility decisions, counts, rulesets and audit records.

## Trust boundaries

| Boundary | Trust |
| --- | --- |
| Local operator and browser | Trusted to operate the tool; still protected from CSRF-style requests |
| X post/reply/OCR content | **Untrusted data**, never instructions or code |
| Adapter output | Untrusted until validated and normalized |
| Installed adapter tools (`twscrape`, `x-cli`, `Scweet`) | Trusted code; supply-chain risk is the operator's |
| Optional AI endpoint | Untrusted output; suggestions only, schema-validated |
| Local filesystem and other local users | Session directories restricted, but same-user access is not preventable |
| Network peers | Untrusted; loopback-only by default |

## Implemented controls

### Network and HTTP

- Default bind is `127.0.0.1:8765`; the server does not add TLS or authentication.
- Mutating requests (`POST`, `PUT`, `DELETE`) require `X-Requested-With: x-vote` and
  `Content-Type: application/json`. Missing header → 403 `missing_client_header`; wrong
  content type → 415.
- No permissive CORS headers are emitted; the UI is same-origin.
- In-memory rate limit: 240 requests per 60 seconds per client address.
- Request bodies are capped at 10 MB; OCR image uploads at 8 MB.
- Security headers: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, and a restrictive CSP:
  `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; ...`
  with `frame-ancestors 'none'`, `base-uri 'none'` and `form-action 'none'`.
- API responses use `Cache-Control: no-store`; static files use `no-cache`.
- Static serving resolves paths under `web/`, rejects traversal and symlinks, and only
  serves existing regular files.
- Export downloads resolve under the session export directory, reject traversal/symlinks
  and only serve the resolved file.
- Unhandled exceptions become generic 500 JSON responses; stack traces are not returned to
  the client.

### Untrusted content and output encoding

- Source text is stored as data and displayed through DOM text nodes or an escaping helper;
  it is never evaluated as HTML/JavaScript.
- HTML exports escape every cell and limitation.
- CSV exports prefix cells beginning with `=`, `+`, `-`, `@`, tab or carriage return with
  an apostrophe to reduce formula-injection risk.
- JSON exports serialize data; they do not embed executable content.
- Agent prompts wrap source text in an explicit untrusted-content block, ignore
  instructions inside it and schema-validate model output.

### Outbound collection

- Adapter URLs are restricted to explicit host allow-lists.
- Schemes are limited to HTTP/HTTPS; embedded credentials are rejected.
- Redirects are disabled.
- Host resolution rejects link-local, multicast and unspecified addresses.
- Requests have timeouts and response-size limits (default 2 MB for source responses;
  AI responses capped at 200 KB).
- Adapter errors are typed (rate limit, permission, credentials missing, source
  unavailable, capability unsupported) and never trigger authentication bypasses.
- No shell interpolation: x-cli is invoked with `create_subprocess_exec` and fixed
  argument arrays. Adapter command probes use fixed commands.
- Live source types require the operator acknowledgement described below.

### Authorization and compliance gate

Live collection from X post/poll/reply/like/repost/quote/mention/hashtag/keyword sources is
blocked unless `XVOTE_AUTHORIZED_USE_ACK` is `yes`/`true`/`1` (or the requirement is
explicitly disabled in configuration). The failure is HTTP 428
`authorized_use_not_acknowledged`. Fixture/manual imports are exempt because they do not
touch X. Changing the gate is a deliberate operator configuration action.

### Credentials and data at rest

- Credentials are read from environment variables only; they are not accepted by the
  configuration YAML, API or UI, and are not serialized into checkpoints.
- `public_dict()` and settings responses omit endpoint values and secrets.
- x-cli cookies are written to a temporary 0600 file for the child process.
- The twscrape account database lives in the session directory; the session directory is
  mode 0700 and checkpoints/evidence files are mode 0600.
- Memory storage is the default. Optional SQLite storage is session-scoped and removed at
  cleanup.
- No telemetry, analytics, automatic upload or cloud synchronization exists. Exports stay
  local.
- Account-level and raw-evidence export fields require explicit confirmation; that
  confirmation is recorded in the export metadata and audit log.

### Lifecycle

- Session cleanup removes directories and reports failures per session. Export files are
  intentionally outside cleanup.
- Valid checkpoints can be restored after an interrupted process; unreadable/orphan
  session directories are discarded and reported.
- `SIGKILL` or host crash can leave temporary artifacts; run `python3 -m xvote cleanup`
  and inspect the session root. Cleanup failures are not silently hidden.

## Residual risks and non-goals

- **No authentication or TLS.** Loopback is the security boundary. Any local user or
  process that can reach the port may act as the operator. Do not bind to `0.0.0.0` or
  expose the port through a proxy without adding authentication, TLS, origin checks and
  host firewalling.
- **Same-user data access.** File permissions do not protect session data from the same
  operating-system user or from root.
- **Installed tools are trusted.** twscrape, x-cli, Scweet, tesseract and their dependencies
  execute with the operator's privileges. Audit and pin them according to your supply-chain
  policy.
- **DNS rebinding.** Host allow-listing and address checks reduce SSRF risk, but the
  resolver result is checked before the HTTP client connects; a hostile DNS server could
  still race the lookup. Use a trusted resolver and keep the Nitter endpoint local.
- **Local Nitter access.** The default Nitter host is loopback by design. If configured to
  a private-network host, that host becomes reachable to the adapter; configure only
  infrastructure you control.
- **AI data egress.** A remote AI endpoint receives selected post text and project names.
  It is disabled by default and requires explicit consent plus an HTTPS endpoint and key.
  Never enable remote AI for content that may not leave your environment.
- **Sensitive exports.** Confirmed account-level exports may contain personal data. Store,
  share and delete them according to applicable law and policy. x-vote cannot prevent a
  spreadsheet application from executing content beyond the CSV-formula mitigation.
- **Log leakage.** The server avoids logging request bodies and credentials, but proxy,
  shell history, crash dumps or operator-added logging may capture secrets. Use a dedicated
  environment and rotate credentials.
- **Malicious evidence.** Text, OCR and manual totals are operator-supplied evidence, not
  cryptographic proof. Review before relying on them.
- **No human verification.** Account IDs are observable identifiers, not proof of unique
  humans. Sybil/multi-account activity cannot be ruled out.

## Deployment checklist

1. Keep the default loopback bind unless you add a properly authenticated reverse proxy.
2. Run as a dedicated unprivileged user; restrict the session/export roots to that user.
3. Do not put real credentials in `config/acquisition.yaml`, source control, screenshots or
   logs.
4. Set `XVOTE_AUTHORIZED_USE_ACK=yes` only after confirming authorization and terms.
5. Validate the installed adapter versions and licenses before live collection.
6. Keep Nitter and any auxiliary tools patched; do not expose Nitter publicly.
7. Review coverage and limitations before publishing a leaderboard.
8. Delete sessions and old exports when no longer needed; verify cleanup output.
9. If remote AI is enabled, use HTTPS, a dedicated key, explicit consent and a data
   handling policy.
10. Treat synthetic demo results as test data; never relabel them as live results.
