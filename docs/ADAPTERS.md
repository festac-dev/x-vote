# Acquisition adapters

x-vote only collects through declared adapters. An adapter must state its capabilities,
authentication tier and availability honestly; the router skips unsupported or
unconfigured adapters and records why.

## Capability matrix

| Adapter | Auth tier | Single post | Replies | Poll aggregate | Likers | Reposters | Enabled by default |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `x-tweet-fetcher` | none | Yes (FxTwitter) | Yes (self-hosted Nitter) | No | No | No | Yes |
| `twscrape` | auth token/session pool | Yes | Yes | Yes | Yes | Yes | Yes, but unavailable without package + account pool |
| `x-cli` | session cookie | Yes | Yes | Yes | Yes | Yes | Yes, but unavailable without binary + cookie |
| `Scweet` | auth token | No declared single-post operation | Yes | **No** | No | Yes | No (opt-in) |
| `manual-import` | none | N/A | CSV/JSON records | Manual total only | No | No | Always available |
| `ocr-evidence` | none | N/A | N/A | OCR text/image totals | No | No | Available; image OCR needs Pillow/pytesseract/tesseract |
| `synthetic-fixture` | none | Fixture only | Fixture only | Fixture only | Fixture only | Fixture only | Demo/test sessions only |

“No” means the adapter does not declare the capability. The router will not invent data by
calling a different method on that adapter. A successful no-poll adapter never produces a
poll aggregate.

## Status in this build environment

| Adapter | Probe status | What was verified |
| --- | --- | --- |
| `x-tweet-fetcher` | Available/configured; Nitter base is loopback `http://127.0.0.1:8788` | Config/probe shape, URL/host validation, response parsers by inspection; **no live FxTwitter or Nitter network test** |
| `twscrape` | Unavailable: package not installed | Router skip/failure behavior; adapter probes; no live twscrape test |
| `x-cli` | Unavailable: binary not found | Router skip/failure behavior; fixed command construction tests; no live x-cli test |
| `Scweet` | Disabled and not installed | Source inspection; no live Scweet test |
| `manual-import` | Available | CSV/JSON parsing, validation, aggregates, limitations |
| `ocr-evidence` | Text path available; image path depends on extras | Text parsing, image magic/limits, OCR-unavailable error path |
| `synthetic-fixture` | Available only for demo/tests | Full demo, ranking and HTTP smoke workflow |

This distinction matters. The adapters are implemented, but **live network behavior against
X is not claimed as tested**. Validate each adapter in your own authorized environment
before relying on its coverage.

## Availability and probes

`GET /api/adapters` returns every constructed adapter with:

- `name`, `auth_tier`, `available`, `unavailable_reason`;
- declared `capabilities`;
- `version` from the installed package/binary where detectable;
- `commands` probe results (`true` confirmed, `false` confirmed absent, `null`/absent
  unknown);
- `license_note`, `compliance_note` and `capability_source`.

Rules:

- Disabled adapter → skipped with a reason.
- Missing binary/module/account pool/cookie → skipped with `credentials_missing` or
  `adapter_unavailable`.
- Missing capability → skipped with `capability_unsupported`.
- Command probes never assume an unverified command works.
- An adapter returning an empty result is recorded as `empty/no_data`; x-vote does not
  treat it as proof that no activity exists.

## Router fallback

The operator order in `config/acquisition.yaml` is respected, then any remaining adapters
are appended deterministically. For each operation:

1. Capability and availability are checked.
2. Each candidate runs with the configured timeout.
3. Rate limits, permission restrictions, missing credentials, source errors, unsupported
   capabilities and unexpected defects are recorded as typed attempts.
4. A later permitted adapter may supply the result; the result is marked `partial` and a
   limitation is attached when an earlier candidate failed or returned empty.
5. If no candidate succeeds, a `CollectionError` is raised. The service records a coverage
   status such as `rate_limited`, `permission_restricted`, `source_unavailable` or
   `partially_complete`.

The router never retries with authentication tricks, never bypasses a CAPTCHA or access
control and never synthesizes missing poll/individual data.

## Adapter-specific setup notes

### x-tweet-fetcher

- Single posts use `api.fxtwitter.com`; replies use a self-hosted Nitter base.
- Public Nitter instances are not supported.
- Nitter responses are parsed as HTML; malformed or empty pages yield no fabricated
  replies.
- Configure `nitter_base` with an explicit reachable instance you control.
- Verify the licenses and policies of FxTwitter and your Nitter deployment.

### twscrape

- Install separately and provide an authorized account pool.
- The adapter creates a session-scoped account database under the session directory.
- Declares poll/replies/likers/reposters, but live behavior is version- and
  access-dependent.
- Rate-limit and auth errors are surfaced; no bypass is attempted.

### x-cli

- Install separately; the default binary name is `x`.
- Session cookie is read from `XCLI_SESSION_COOKIE` and written to a temporary 0600 file.
- Fixed read-only commands: post, replies, poll, likers, retweeters, with JSONL output.
- The `commands` probe runs `x --help` and reports unknown rather than guessing.
- No shell is used.

### Scweet

- Opt in with `scweet.enabled: true` and `SCWEET_AUTH_TOKEN`.
- Declares replies and reposters only. Polls are explicitly unsupported.
- Scweet's API varies by version; treat live behavior as unverified.

### Manual import

CSV/JSON rows are validated and normalized. A record with a vote count and no account is
an aggregate; a record with an account defaults to an individual reply unless a method is
specified. Invalid votes, methods, confidence labels and missing projects are rejected.
Manual aggregates are labelled and require review by default.

### OCR evidence

- Text lines must look like `Project: N votes` (separator variants accepted).
- Images must be PNG/JPEG/WebP, valid base64, under 8 MB, and pass image verification.
- OCR results are always aggregate, low-confidence/manually supplied and require review.
- If Pillow/pytesseract/tesseract is unavailable, the API returns `ocr_unavailable` with
  a clear message rather than pretending OCR succeeded.

### Synthetic fixture

- Only available in demo/test sessions, never for live sources.
- Every record is marked `is_test_data=True` and displayed with the synthetic banner.
- Fixtures are never silently mixed with live data.

## Consent and licensing

Open-source code does not grant permission to access any account or instance. Operators
must have authorized access, comply with each tool's license, X's applicable terms and
access controls, self-hosted instance policies and rate limits. The live-collection gate is
described in [SECURITY.md](SECURITY.md).
