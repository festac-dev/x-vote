# Installation

## Base requirements

- Python 3.11 or newer (tested with 3.14.4).
- PyYAML 6+.
- Linux, macOS or a Termux-like environment with a Python 3 runtime.
- A modern browser for the local UI.

From the repository root, the application runs without installation:

```bash
python3 -m xvote --help
python3 -m xvote serve
```

Optional editable install:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
python3 -m pip install -e ".[test]"
```

No frontend build step, CDN, database server or cloud account is required. Static files
are served directly from `web/`.

## Configuration files

- `config/acquisition.yaml` — adapter preference, timeouts, concurrency, agent limits and
  compliance controls. Never place credentials here.
- `.env.example` — environment variable template. CLI commands automatically load
  `.env` from the current directory or project root. Already-exported variables take
  precedence; keep the file private (`chmod 600 .env`).

Useful environment variables:

| Variable | Purpose |
| --- | --- |
| `XVOTE_HOST` | Bind address; default `127.0.0.1` |
| `XVOTE_PORT` | Bind port; default `8765` |
| `XVOTE_SESSION_ROOT` | Temporary session root; default private system temp directory |
| `XVOTE_EXPORT_DIR` | Export directory; default `~/.x-vote/exports` (or `$HOME` equivalent) |
| `XVOTE_ENV_FILE` | Alternate `.env` path; otherwise the current directory then project root is checked |
| `XVOTE_AUTHORIZED_USE_ACK` | Set to `yes` only for authorized live collection; restart the server after changing `.env` |
| `TWSCRAPE_ACCOUNTS` | twscrape account pool, one `username:password:email:email_password` per line |
| `XCLI_SESSION_COOKIE` | x-cli session cookie, written to a temporary 0600 file |
| `SCWEET_AUTH_TOKEN` | Scweet auth token, only when Scweet is enabled |
| `XVOTE_AI_ENDPOINT` / `XVOTE_AI_KEY` / `XVOTE_AI_CONSENT` | Optional AI provider |

Keep secrets out of shell history where possible (for example, use a service manager or
a private environment file that you do not commit).

## 1. Run the deterministic demo first

```bash
python3 -m xvote demo
```

This creates a synthetic fixture session, collects through the fixture adapter, runs the
deterministic agents, ranks the projects, imports a manual aggregate and leaves the
session available for inspection. It requires no X access and no credentials. The output
is explicitly labelled synthetic test data.

Start the UI:

```bash
python3 -m xvote serve
```

Then open `http://127.0.0.1:8765` and use **Load synthetic demo** if you want the same
data inside the UI.

## 2. Self-hosted Nitter (replies for `x-tweet-fetcher`)

`x-tweet-fetcher` uses:

- `api.fxtwitter.com` for a single post (no credentials).
- A **self-hosted** Nitter instance for replies.

Public Nitter instances are not used. Configure the base URL in
`config/acquisition.yaml`:

```yaml
acquisition:
  x_tweet_fetcher:
    enabled: true
    nitter_base: "http://127.0.0.1:8788"
```

Deploy and operate Nitter according to its project license and the policies of the
instance you control. Ensure it is reachable from the x-vote process but not exposed to
untrusted networks. The adapter validates the configured hostname and refuses arbitrary
outbound targets. If Nitter is unavailable, replies fail with a source-unavailable
status; no data is fabricated and the coverage record remains incomplete.

If FxTwitter or your Nitter instance cannot be reached, x-vote reports the failure and
offers fallback adapters only if they are installed and permitted.

## 3. twscrape (optional, authenticated account pool)

twscrape is not bundled. Install it separately according to its project documentation:

```bash
python3 -m pip install twscrape
```

Provide an authorized account pool. Either export `TWSCRAPE_ACCOUNTS` as lines of
`username:password:email:email_password`, or as a JSON array/object accepted by the
loader, or pre-populate the session-scoped DB path configured in
`acquisition.twscrape.accounts_db`. The default path is
`${SESSION_DIR}/twscrape_accounts.db`.

`twscrape` is enabled in the default configuration but reports unavailable until both the
package and an account pool exist. Account credentials are held only in memory and the
temporary session directory, which is cleaned on exit. Do not reuse credentials outside
your authorization.

The adapter declares poll, replies, likers and reposters. Its live behavior depends on
the installed twscrape version and X's current access controls. It was not installed in
this build environment, so only probes and failure handling are verified here.

## 4. x-cli (optional)

Install the `x` command from its own project and make it available on `PATH`, or set an
absolute path in `config/acquisition.yaml`:

```yaml
acquisition:
  x_cli:
    enabled: true
    binary: "x"
```

Provide an authorized session cookie through `XCLI_SESSION_COOKIE`. x-vote writes it to
a temporary file with mode 0600 for the child process and passes `X_SESSION_FILE` and
`XCLI_SESSION_FILE`. Commands are fixed, read-only invocations with JSONL output; there is
no shell interpolation. If the binary or cookie is absent, the adapter is skipped.

x-cli was not installed in this build environment. Validate the installed command names
with `/api/adapters`, which probes `x --help`; unverified commands are reported as unknown,
never assumed to work.

## 5. Scweet (optional, disabled by default)

Install Scweet separately, then opt in:

```yaml
acquisition:
  scweet:
    enabled: true
```

Export `SCWEET_AUTH_TOKEN`. The adapter declares replies and reposters; it does **not**
declare polls, so poll data is never invented. Scweet's API surface varies by version;
treat live behavior as unverified until exercised in your environment.

## 6. Manual CSV/JSON import

No extra dependency. In the UI, use Manual Import, or call the API with a payload. Expected
columns/keys:

```csv
project,votes,method,account_id,source_url,event_timestamp,confidence,note
Alpha,120,manual_total,,https://example.test/manual,2026-01-01T00:00:00Z,moderate,Operator total
Beta,3,reply,12345,https://x.com/user/status/1,2026-01-01T00:00:00Z,high,
```

A row with a vote count and no account is treated as an aggregate. A row with an
`account_id` and no explicit method defaults to an individual reply. Manual aggregates
are labelled as manually supplied and require review by default. Empty/negative vote
counts, unknown methods, bad confidence labels and missing `project` columns are rejected.

## 7. OCR evidence (optional)

Text OCR requires no extra runtime beyond the application:

```bash
python3 -m xvote serve
# Use OCR text import in the UI
```

Image OCR requires Pillow, pytesseract and the system `tesseract` binary:

```bash
python3 -m pip install pillow pytesseract
# install tesseract with your OS package manager
```

Supported uploads are PNG, JPEG and WebP up to 8 MB. OCR output is always treated as
low-confidence manually supplied evidence and requires operator review. OCR is optional;
the UI reports a clear error if the engine is unavailable.

## 8. Optional AI provider

The default `agents.ai_mode: none` is fully deterministic and makes no network model
calls. To enable suggestions:

```yaml
agents:
  ai_mode: "local"          # or "remote"
  ai_endpoint_env: "XVOTE_AI_ENDPOINT"
  ai_key_env: "XVOTE_AI_KEY"
  ai_consent_env: "XVOTE_AI_CONSENT"
  ai_auth_header: "Authorization"  # provider-specific, e.g. "X-API-Key"
  ai_auth_prefix: ""               # optional provider-specific scheme
```

```bash
export XVOTE_AI_ENDPOINT=http://127.0.0.1:11434/v1/chat/completions
export XVOTE_AI_CONSENT=yes
# remote endpoints also require XVOTE_AI_KEY
```

Local endpoints must be loopback. Remote endpoints must use HTTPS. Model output is
schema-validated and stored only as a suggestion; it can never change counts, rules,
permissions or tool access.

## 9. Verify the installation

```bash
python3 -m xvote demo
pytest
ruff check .
mypy xvote
```

See [TEST_REPORT.md](TEST_REPORT.md) for expected results and
[ADAPTERS.md](ADAPTERS.md) for the exact capability matrix.
