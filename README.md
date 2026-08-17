# Usage-puller

Read the real-time quota of your AI coding assistants and get back **one compact block
of text you can copy anywhere** — from a terminal, from a GitHub Actions step summary, or
from the GitHub Mobile app on your phone.

Covers three providers in one shot:

| Provider | What is reported | Auth mechanism |
| --- | --- | --- |
| **Anthropic Claude Code** | 5-hour session window + reset timer, 7-day weekly cap, any scoped per-model pool (e.g. Fable) with its severity flag, pay-as-you-go extra credits | CLI OAuth token |
| **OpenAI Codex** | Month-to-date spend against the hard limit, plan, rolling request/token allowance | API key |
| **Google Antigravity (`agy`)** | Daily tier, Vertex AI quota consumption, AI Platform enablement state | GCP service account or ADC |

```text
=================================================================
           AI ASSISTANT QUOTA SUMMARY (2026-08-17 09:12 UTC)
=================================================================
[Claude Code]  5h: 2% (resets 8h 49m) | 7d: 47% (resets 3d 22h)
               Fable 7d: 81% (warning) | Extra credits: off
[OpenAI Codex] Limit Used: 42.5% | Spent: $42.50 / $100.00
               Plan: Pay-as-you-go | Requests: 0.1% used
[Antigravity]  Daily Tier: Normal
               Quota: 15% used (AI Platform: ENABLED)
=================================================================
```

Any provider you have not configured simply says so, and the rest still report:

```text
[OpenAI Codex] Status: UNCONFIGURED (Missing OPENAI_API_KEY)
```

---

## Contents

- [Quick start](#quick-start)
- [Architecture](#architecture)
- [Authentication, provider by provider](#authentication-provider-by-provider)
- [Step 1 — extract your tokens](#step-1--extract-your-tokens)
- [Step 2 — add the GitHub Secrets](#step-2--add-the-github-secrets)
- [Step 3 — run it from GitHub Mobile on Android](#step-3--run-it-from-github-mobile-on-android)
- [Local and Termux usage](#local-and-termux-usage)
- [CLI reference](#cli-reference)
- [Output format](#output-format)
- [Error handling and degradation](#error-handling-and-degradation)
- [Security notes](#security-notes)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

---

## Quick start

```bash
git clone https://github.com/flavio-mercati/usage-puller.git
cd usage-puller
pip install -r requirements.txt

# 1. See which credentials this machine already has
python extract_tokens.py

# 2. Run against everything it found
python check_usage.py
```

For the mobile workflow, jump to [Step 2](#step-2--add-the-github-secrets).

---

## Architecture

```
                       ┌───────────────────────────────┐
                       │        check_usage.py         │
                       │                               │
  ~/.claude/           │  ┌─────────────────────────┐  │
  .credentials.json ──▶│  │ ClaudeResolver          │──┼──▶ api.anthropic.com
  CLAUDE_REFRESH_TOKEN │  │  refresh → access token │  │    /api/oauth/usage
                       │  └─────────────────────────┘  │
                       │  ┌─────────────────────────┐  │
  OPENAI_API_KEY ─────▶│  │ CodexResolver           │──┼──▶ api.openai.com
                       │  │  billing → rate limits  │  │    /dashboard/billing/*
                       │  └─────────────────────────┘  │    /v1/models
                       │  ┌─────────────────────────┐  │
  GCP_SERVICE_       ──▶│  │ AntigravityResolver     │──┼──▶ serviceusage + monitoring
  ACCOUNT_JSON / ADC   │  │  google-auth → bearer   │  │    .googleapis.com
                       │  └─────────────────────────┘  │
                       │              │                │
                       │              ▼                │
                       │      SummaryFormatter         │
                       └───────────────┬───────────────┘
                                       │
                 ┌─────────────────────┼─────────────────────┐
                 ▼                     ▼                     ▼
            stdout (ASCII)     $GITHUB_STEP_SUMMARY      --json / --json-out
```

Design rules the code follows:

- **One resolver per provider**, each a `QuotaResolver` subclass implementing `fetch()`.
  `resolve()` wraps `fetch()` and converts *any* failure into a degraded report, so a
  broken provider can never abort the run.
- **Every network call is bounded** by a timeout (default 10s, clamped to 3–60s).
- **Tolerant JSON parsing.** Provider payloads are searched by field *alias* rather than
  a hard-coded path, so a renamed field degrades to `n/a` instead of a crash. Candidate
  windows are ranked by nesting depth so an account-wide `7d` figure is never satisfied
  by a per-model sub-pool value.
- **Optional dependencies are truly optional.** `requests`, `google-auth`,
  `google-api-python-client`, `python-dotenv` and `pydantic` are each probed at import;
  the script falls back to `urllib`, skips `.env` loading, and reports Antigravity as
  `UNCONFIGURED` rather than failing. A bare `python check_usage.py` works on a stock
  interpreter — which is what makes the Termux path viable.

### Files

| File | Purpose |
| --- | --- |
| `check_usage.py` | The engine: resolvers, HTTP layer, parsers, formatter, CLI |
| `extract_tokens.py` | Local discovery utility — finds credentials, prints the secrets to set |
| `.github/workflows/usage.yml` | `workflow_dispatch` workflow writing to `$GITHUB_STEP_SUMMARY` |
| `requirements.txt` | Dependencies (all optional at runtime) |
| `.env.example` | Template for every supported variable |
| `tests/test_check_usage.py` | Offline fixture tests for parsers, resolvers and degradation |

---

## Authentication, provider by provider

### Anthropic Claude Code

**Why not browser cookies.** Scraping `claude.ai` with a copied session cookie is
fragile: the cookie is short-lived, and the edge applies bot challenges that return an
HTML interstitial instead of JSON. This project instead uses **the same OAuth token the
`claude` CLI uses**, against `api.anthropic.com` — a plain JSON API with no challenge
layer.

> **The tool will not rotate your login.** Anthropic rotates refresh tokens: exchanging
> one issues a replacement and invalidates the old. A quota checker that refreshed
> carelessly would therefore log the `claude` CLI out. So: a valid access token is
> **never** traded for a new one; when a refresh is unavoidable, the replacement is
> written back to the credential file; and a Keychain-stored credential — which cannot
> be rewritten safely from here — is **refused** rather than consumed. See
> [Token safety](#token-safety).

The resolver walks this chain and stops at the first success:

1. `CLAUDE_ACCESS_TOKEN` / `CLAUDE_CODE_OAUTH_TOKEN` (short-lived; used directly)
2. `CLAUDE_REFRESH_TOKEN` → exchanged for an access token at
   `https://console.anthropic.com/v1/oauth/token`
3. `~/.claude/.credentials.json` → `claudeAiOauth.accessToken`, refreshed via
   `claudeAiOauth.refreshToken` when expired
4. macOS Keychain entry `Claude Code-credentials`

Then it queries, in order:

- `GET https://api.anthropic.com/api/oauth/usage`
- `GET https://api.anthropic.com/v1/organizations/{CLAUDE_ORG_ID}/usage` (only if
  `CLAUDE_ORG_ID` is set — a fallback for when the first endpoint changes)

Parsed out of the response: the 5-hour rolling window percentage and reset time, the
7-day cap percentage, any populated per-model sub-pool, and extra pay-as-you-go credit
usage when the account has it enabled.

The authoritative structure is the `limits` array, which is self-describing:

```json
{"kind": "session",     "group": "session", "percent": 5,  "resets_at": "...", "scope": null},
{"kind": "weekly_all",  "group": "weekly",  "percent": 45, "resets_at": "...", "scope": null},
{"kind": "weekly_opus", "group": "weekly",  "percent": 38, "is_active": true}
```

`kind: session` and `kind: weekly_all` become the `5h` and `7d` lines. Anything else is a
**scoped sub-pool**, labelled from its `scope` — which is a nested object, not a string:

```json
{"kind": "weekly_scoped", "group": "weekly", "percent": 81, "severity": "warning",
 "scope": {"model": {"id": null, "display_name": "Fable"}, "surface": null},
 "is_active": true}
```

That renders as `Fable 7d: 81% (warning)`. No model name is hard-coded, so any scoped
pool the provider emits appears automatically — `weekly_opus` becomes `Opus 7d`, a pool
scoped to Sonnet becomes `Sonnet 7d`. The older flat layout (`five_hour` / `seven_day`
objects, `seven_day_fable` keys) is still parsed as a fallback.

Three details that matter if you are deliberately pacing a pool:

- **`severity` is surfaced.** The provider's own `warning` / `critical` flag is appended
  to the percentage, so the cue to change behaviour is visible without arithmetic.
- **Both countdowns show.** The session reset and the weekly reset each appear once.
  Every 7d pool shares one weekly reset, so it is not repeated per pool.
- **`remaining_percent` is in the JSON.** `--json` reports headroom per window alongside
  the used percentage — the number you want when the goal is to exhaust a pool before it
  resets.

> **Empty sub-pools.** The endpoint also returns per-model slots as top-level keys
> (`seven_day_opus`, `seven_day_sonnet`, `seven_day_cowork`, plus codenamed slots), and
> these are often all `null` even while a scoped pool is live inside `limits[]`. When no
> sub-pool has data you get an explicit `Sub-pools: none active (N reported empty)`
> rather than a silently missing line, so a blank is never ambiguous between "no usage"
> and "the parser missed it".

### OpenAI Codex

Authenticates with `OPENAI_API_KEY`, optionally scoped by `OPENAI_ORG_ID` and
`OPENAI_PROJECT_ID`. Two sources, tried in order:

1. **Billing** — `GET /dashboard/billing/subscription` for the hard limit and plan, and
   `GET /dashboard/billing/usage` for month-to-date spend (returned in cents). This is
   the authoritative view, but OpenAI only exposes it to some credential classes.
2. **Rate-limit headers** — if billing refuses the key, `GET /v1/models` is used for the
   live `x-ratelimit-*` headers, giving a rolling request and token allowance.

If billing works you get `Limit Used: 18.5% | Spent: $42.50 / $100.00`. If only headers
work you get `Requests: 25% used (375/500 left)` plus a note. If neither works, `ERROR`.

> A ChatGPT-plan Codex CLI login (`~/.codex/auth.json`) stores an *access token*, not an
> API key. `extract_tokens.py` will find it and warn you: billing endpoints will refuse
> it, so create a real key at <https://platform.openai.com/api-keys>.

### Google Antigravity (`agy`)

Antigravity runs on Vertex AI, so the observable quota signals are GCP's:

- **Service Usage API** — `GET /v1/projects/{project}/services/aiplatform.googleapis.com`
  reports `ENABLED` / `DISABLED`.
- **Cloud Monitoring API** — the `serviceruntime.googleapis.com/quota/rate/net_usage` and
  `serviceruntime.googleapis.com/quota/limit` time series for that service, aligned to
  hourly maxima over a look-back window (default 24h). The reported percentage is the
  worst `usage / limit` ratio across quota metrics.

Credentials come from `GCP_SERVICE_ACCOUNT_JSON` (raw JSON, base64 of that JSON, or a
path to the key file), then Application Default Credentials, then
`gcloud auth print-access-token`. Required roles:

- `roles/serviceusage.serviceUsageViewer`
- `roles/monitoring.viewer`

**Daily Tier** is derived from consumption — `Normal` below 75%, `Reduced` at 75–90%,
`Exhausted` at 90%+, `UNAVAILABLE` if the API is disabled. Google does not publish an
Antigravity tier endpoint, so this is a local heuristic; pin it with `AGY_TIER_OVERRIDE`
if you want a fixed label.

---

## Step 1 — extract your tokens

Run this on the machine where you already use the CLIs. It reads local config only —
nothing is uploaded, and nothing is written unless you redirect the output.

```bash
python extract_tokens.py
```

```text
========================================================================
  USAGE-PULLER - LOCAL CREDENTIAL DISCOVERY
  host: Linux x86_64 | python 3.11.15
========================================================================
  values are MASKED - re-run with --reveal to print them in full
------------------------------------------------------------------------

## Claude Code
  [OK  ] CLAUDE_REFRESH_TOKEN  (required)
         value : sk-ant...UUUU (37 chars)
         from  : /home/you/.claude/.credentials.json
         note  : the only Claude secret you actually need in GitHub; plan reported as max
         warn  : /home/you/.claude/.credentials.json is world/group readable (mode 644); consider chmod 600
  [OK  ] CLAUDE_ORG_ID  (optional)
         value : 111111...5555 (36 chars)
         from  : /home/you/.claude.json
...
```

It scans:

| Provider | Locations checked |
| --- | --- |
| Claude | `CLAUDE_*` env vars, `~/.claude/.credentials.json`, `~/.config/claude/.credentials.json`, macOS Keychain, `~/.claude.json` (for the org UUID) |
| Codex | `OPENAI_API_KEY` / `CODEX_API_KEY`, `~/.codex/auth.json`, `~/.codex/config.json`, `~/.config/codex/auth.json` |
| Antigravity | `GCP_SERVICE_ACCOUNT_JSON`, `GOOGLE_APPLICATION_CREDENTIALS`, `~/.config/gcloud/application_default_credentials.json`, `gcloud config get-value project`, `~/.antigravity/config.json`, `~/.agy/config.json` |

Output formats:

```bash
python extract_tokens.py --reveal                  # full values, ready to paste
python extract_tokens.py --reveal --format env      # KEY=value lines
python extract_tokens.py --reveal --format env > .env
python extract_tokens.py --reveal --format gh        # gh secret set commands
python extract_tokens.py --format json               # machine readable
python extract_tokens.py --only claude               # one provider
```

The `gh` format writes a runnable script — the fastest path to a configured repo:

```bash
python extract_tokens.py --reveal --format gh | bash
```

---

## Step 2 — add the GitHub Secrets

Repository → **Settings** → **Secrets and variables** → **Actions** → **New repository
secret**. Create the ones you want; every secret is optional and a missing one only
degrades its own provider.

| Secret | Required for | Where it comes from |
| --- | --- | --- |
| `CLAUDE_REFRESH_TOKEN` | Claude Code | `~/.claude/.credentials.json` → `claudeAiOauth.refreshToken` |
| `CLAUDE_ORG_ID` | *optional* | Organisation UUID; enables the `/v1/organizations/{id}/usage` fallback |
| `CLAUDE_OAUTH_CLIENT_ID` | *optional* | Only if Anthropic changes the CLI OAuth client id |
| `OPENAI_API_KEY` | OpenAI Codex | <https://platform.openai.com/api-keys> |
| `OPENAI_ORG_ID` | *optional* | Needed when the key spans several organisations |
| `OPENAI_PROJECT_ID` | *optional* | Recommended for `sk-proj-…` keys |
| `GCP_SERVICE_ACCOUNT_JSON` | Antigravity | Service account key JSON, pasted whole |
| `GCP_PROJECT_ID` | Antigravity | Project hosting `aiplatform.googleapis.com` |
| `CLAUDE_SECRET_UPDATER_PAT` | *optional* | Fine-grained PAT with `Secrets: write` on this repo, so the workflow can store rotated Claude tokens — see [Token safety](#token-safety) |

Two repository **variables** (not secrets) tune Antigravity behaviour:
`AGY_TIER_OVERRIDE` and `AGY_LOOKBACK_HOURS`.

<details>
<summary>Creating the GCP service account</summary>

```bash
PROJECT_ID=your-project
gcloud iam service-accounts create usage-puller \
  --display-name="usage-puller quota reader" --project "$PROJECT_ID"

SA="usage-puller@${PROJECT_ID}.iam.gserviceaccount.com"
for ROLE in roles/serviceusage.serviceUsageViewer roles/monitoring.viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA}" --role="$ROLE"
done

gcloud iam service-accounts keys create key.json --iam-account "$SA"
# paste the contents of key.json into GCP_SERVICE_ACCOUNT_JSON, then:
rm key.json
```

</details>

---

## Step 3 — run it from GitHub Mobile on Android

The workflow exists to be triggered from a phone, and the result is written to the run
summary so it is one long-press away from your clipboard.

1. Open the **GitHub** app (Android or iOS) and go to your `usage-puller` repository.
2. Tap the **☰ / overflow** menu and choose **Actions**.
3. Select the **AI Usage Snapshot** workflow.
4. Tap **Run workflow**. Leave the inputs alone for the default "everything" run, or set:
   - **providers** — `all`, or a list like `claude,codex` / `agy`
   - **timeout** — per-request seconds (3–60)
   - **verbose** — log every HTTP request into the job log
5. Wait ~30 seconds and open the finished run. The **Summary** tab shows:

   ```text
   ### AI quota snapshot

   =================================================================
              AI ASSISTANT QUOTA SUMMARY (2026-08-17 09:12 UTC)
   =================================================================
   [Claude Code]  5h: 2% (resets 8h 49m) | 7d: 47% (resets 3d 22h)
   ...
   ```

6. **Long-press the code block** and tap **Copy**. Because the summary is written as a
   fenced `text` block, the mobile app offers a copy action for the whole block rather
   than making you select lines by hand.

Every run also attaches `usage-snapshot.json` as an artifact (7-day retention) if you
want the structured numbers.

> **Tip.** GitHub Mobile keeps recently-run workflows near the top of the Actions list,
> so after the first run it is: Actions → AI Usage Snapshot → Run workflow → Copy.

### Running it on a schedule instead

The workflow is deliberately `workflow_dispatch`-only so it never burns Actions minutes
on its own. To poll automatically, add a `schedule` trigger:

```yaml
on:
  workflow_dispatch:
    # ... existing inputs ...
  schedule:
    - cron: "0 */6 * * *" # every six hours, UTC
```

---

## Local and Termux usage

### macOS / Linux / Windows

```bash
pip install -r requirements.txt
cp .env.example .env        # optional; env vars work just as well
python check_usage.py
```

With no `.env` and no exported variables, the script still finds
`~/.claude/.credentials.json`, `~/.codex/auth.json` and your gcloud ADC automatically —
so on a machine where you already use the CLIs, `python check_usage.py` just works.

### Android Termux

```bash
pkg update && pkg install python git
git clone https://github.com/flavio-mercati/usage-puller.git
cd usage-puller

# Minimal: no pip installs needed at all — check_usage.py falls back to urllib
python check_usage.py --only claude,codex

# Optional, for Antigravity support (needs a compiler for cryptography):
pkg install rust binutils
pip install -r requirements.txt
```

Claude and Codex work on a bare Termux Python with zero dependencies. Antigravity needs
`google-auth`, which pulls in `cryptography` — that requires a Rust toolchain on Termux,
so install it only if you need GCP quota. Without it, Antigravity reports `UNCONFIGURED`
and the other two still report normally.

A handy shell alias:

```bash
echo "alias quota='python ~/usage-puller/check_usage.py'" >> ~/.bashrc
```

### Restricted-egress environments

If you run this behind an egress allowlist, permit these hosts:

| Host | Needed for |
| --- | --- |
| `api.anthropic.com` | Claude usage endpoint |
| `console.anthropic.com` | Claude OAuth token refresh |
| `api.openai.com` | Codex billing and rate limits |
| `oauth2.googleapis.com` | GCP token exchange |
| `serviceusage.googleapis.com`, `monitoring.googleapis.com` | Antigravity quota |

---

## CLI reference

### `check_usage.py`

```
--only PROVIDERS       comma list: claude, codex, antigravity (aliases: agy, openai,
                       anthropic, google, gcp). Default: all three.
--timeout SECONDS      per-request timeout, clamped to 3-60 (default 10)
--json                 emit JSON instead of the ASCII block
--json-out PATH        also write JSON to PATH (one API round-trip for both outputs)
--raw                  include raw provider payloads in JSON output
--width N              ASCII block width (default 65)
--github-summary[=PATH]  append the block to $GITHUB_STEP_SUMMARY (or PATH)
--emit-rotated-token PATH
                       write a rotated refresh token to PATH (mode 600) when it cannot
                       be written back to its source; the value is never logged
--no-refresh           never exchange a refresh token; read-only against whatever
                       access token already exists
--allow-refresh        refresh even when the rotated token cannot be written back
                       (may log the Claude CLI out — see Token safety)
--strict               exit 1 if any selected provider is unconfigured or errored
--strict-schema        validate the output payload and report violations on stderr
--env-file PATH        load variables from a .env file first
-v, --verbose          log every HTTP request to stderr
--version              print the version
```

Exit codes: `0` always, unless `--strict` is passed (then `1` when a provider is
degraded) or the arguments are invalid (`2`).

### `extract_tokens.py`

```
--reveal               print full secret values (default masks them)
--format FORMAT        report (default) | env | json | gh
--only PROVIDERS       claude, codex, antigravity
--version              print the version
```

Exit codes: `0` if at least one credential was found, `2` if none were.

### Environment variables

See [`.env.example`](.env.example) for the annotated list. Beyond the secrets in the
table above:

| Variable | Effect |
| --- | --- |
| `USAGE_HTTP_TIMEOUT` | Default per-request timeout (overridden by `--timeout`) |
| `ANTHROPIC_USAGE_BASE_URL` | Point the Claude resolver at a proxy |
| `OPENAI_BASE_URL` | Point the Codex resolver at a gateway or mock |
| `CLAUDE_CREDENTIALS_PATH`, `CODEX_CONFIG_PATH` | Non-standard credential locations |
| `AGY_LOOKBACK_HOURS` | Cloud Monitoring look-back window (default 24) |
| `AGY_TIER_OVERRIDE` | Pin the reported daily tier |
| `USAGE_PREFER_GOOGLE_CLIENT` | Use `google-api-python-client` for Service Usage instead of the direct REST call (adds a discovery fetch) |

---

## Output format

A fixed-width monospace block, 65 columns by default, chosen so it survives a phone
clipboard, a terminal, and a Markdown code fence without reflowing:

```text
=================================================================
           AI ASSISTANT QUOTA SUMMARY (YYYY-MM-DD HH:MM UTC)
=================================================================
[Claude Code]  5h: 2% (resets 8h 49m) | 7d: 47% (resets 3d 22h)
               Fable 7d: 81% (warning)
[OpenAI Codex] Limit Used: 18.5% | Spent: $42.50 / $100.00
[Antigravity]  Daily Tier: Normal | Quota: 15.0% used (AI Platform: ENABLED)
=================================================================
```

Fields are joined by ` | `. When a provider has more fields than fit, the line wraps
**on field boundaries only** and continuation lines are indented under the label — a
value is never split in half. Only the 5-hour session window carries a countdown; four
simultaneous reset timers stopped being copiable.

`--json` gives the same data structurally:

```json
{
  "generated_at": "2026-08-17T09:12:44Z",
  "tool": "usage-puller",
  "version": "1.0.0",
  "providers": [
    {
      "provider": "claude",
      "label": "Claude Code",
      "status": "OK",
      "source": "https://api.anthropic.com/api/oauth/usage",
      "elapsed_ms": 412,
      "windows": [
        {"label": "5h", "percent": 24.0, "resets_at": "2026-08-17T11:22:44Z",
         "resets_in_seconds": 7800, "used": null, "limit": null, "unit": null},
        {"label": "Fable 7d", "percent": 81.0, "resets_at": null,
         "remaining_percent": 19.0, "severity": "warning"}
      ],
      "facts": {"Extra credits": "$3.40 / $50.00", "Plan": "max"},
      "notes": ["credentials: /home/you/.claude/.credentials.json"]
    }
  ],
  "degraded": []
}
```

---

## Error handling and degradation

Nothing a provider does can take down the run. Each resolver's `resolve()` catches
configuration errors, HTTP errors and unexpected exceptions, and emits one of:

| Status | Meaning | Example line |
| --- | --- | --- |
| `OK` | Everything parsed | `5h: 24% (resets 2h 10m) \| 7d: 62%` |
| `PARTIAL` | Authenticated, but some data was unavailable | `Requests: 25% used (375/500 left) \| Note: billing limits not exposed to this key` |
| `UNCONFIGURED` | No usable credentials | `Status: UNCONFIGURED (Missing OPENAI_API_KEY)` |
| `ERROR` | Credentials present, request or parse failed | `Status: ERROR (HTTP 400: refresh token expired)` |

Specifically handled:

- **Expired tokens** — a rejected refresh reports the provider's own
  `error_description` rather than a stack trace.
- **HTML instead of JSON** (bot challenge, captive portal, proxy error page) — reported
  as an `ERROR` with a truncated body, not a `JSONDecodeError`.
- **Renamed or missing fields** — alias-based lookup; unrecognised payloads become
  `PARTIAL`, and individual missing values render as `n/a`.
- **Timeouts and DNS/TLS failures** — bounded per request and reported by exception type.
- **Broken native dependencies** — optional imports are guarded against
  `BaseException`, not just `ImportError`, because a half-installed `pyo3`-backed wheel
  (`cryptography`, reached via `google-auth`) raises `PanicException`, which subclasses
  `BaseException` and would otherwise sail through an `except Exception` handler and
  kill the process.
- **Ambiguous percentages** — a `0.62` under a field named `*_ratio` becomes 62%, but
  under a field named `utilization` it stays 0.62%, so a genuine "1%" is never inflated
  to 100%.
- **Sub-pool shadowing** — a `seven_day` value nested inside a per-model container can
  never satisfy the account-wide 7-day window.

---

## Token safety

Anthropic's OAuth refresh tokens **rotate**: exchanging one returns a replacement and
invalidates the token you presented. A naive quota checker that refreshes on every run,
discarding the replacement, will silently invalidate the credential store the `claude`
CLI reads — and the next `claude` command fails with
`OAuth session expired and could not be refreshed`.

This happened during development, on a real account, which is why the behaviour below is
enforced rather than merely documented.

| Credential source | Behaviour when the access token is still valid | Behaviour when it has expired |
| --- | --- | --- |
| Any | Used as-is. **No refresh is attempted.** | see below |
| `CLAUDE_ACCESS_TOKEN` env | Used as-is | Reported as an error; nothing to refresh with |
| `CLAUDE_REFRESH_TOKEN` env (CI) | n/a | Refreshed. If the provider rotates, a `WARNING` note says the stored secret is now stale |
| `~/.claude/.credentials.json` | Used as-is | Refreshed, and the rotated token is **written back atomically**, preserving every unrelated field and re-applying mode `600` |
| macOS Keychain | Used as-is | **Refused.** The Keychain cannot be rewritten safely from here, so consuming the token would log the CLI out. Run `claude -p ok` to let the CLI refresh it, then retry |

With `--emit-rotated-token PATH`, any rotation that cannot be written back to its own
source is written to `PATH` instead, so a caller that *can* store it (a CI workflow
updating its own secret) is able to.

Three flags control this:

- `--no-refresh` — never refresh under any circumstance. The safest mode for a monitoring
  cron job: it can only read a token that already exists.
- `--allow-refresh` — override the Keychain refusal. Only use this if you accept
  re-running `claude` to log back in afterwards.
- `--emit-rotated-token PATH` — when a rotation cannot be written back to its own source,
  write the replacement to `PATH` with mode `600` so the caller can store it. Only the
  path is reported; the value never reaches stdout, the summary, the JSON payload or a
  log. This is how CI keeps itself alive — see below.

### Keeping CI alive across rotations

A workflow cannot write its own secrets, so a rotated token would strand the next run.
The workflow therefore runs `check_usage.py --emit-rotated-token "$RUNNER_TEMP/..."` and a
follow-up step pushes the value into the secret with `gh secret set`, registering it with
`::add-mask::` first so it cannot appear in the log.

That step needs a **fine-grained PAT with `Secrets: write`** on this repository, stored as
the secret `CLAUDE_SECRET_UPDATER_PAT`. Without it the run still succeeds and emits a
warning telling you to re-seed manually.

> **This does not stop CI from logging out your local CLI.** One refresh-token chain
> cannot be shared: when the workflow rotates the token, whatever your laptop holds
> becomes invalid, and `claude` will need to log in again. Keeping CI self-sufficient and
> keeping your laptop logged in requires **two independent logins** — authenticate
> somewhere separate (a container or VM) and put *that* refresh token in the secret,
> leaving your laptop's own session untouched. Otherwise expect to re-run `claude` after
> each workflow run.

If you are ever logged out by a token rotation, the recovery is simply to re-authenticate:

```bash
claude
```

## Security notes

- **Nothing is transmitted anywhere except the three providers' own APIs.** There is no
  telemetry and no third-party endpoint.
- `extract_tokens.py` **masks by default**; full values require `--reveal`, and it warns
  on stderr when secrets have been printed to a TTY.
- `.gitignore` already covers `.env`, `*-service-account*.json`, `credentials.json` and
  the generated `usage-snapshot.json`.
- The workflow declares `permissions: contents: read` — it needs nothing else — and
  passes `workflow_dispatch` inputs through **environment variables** rather than shell
  interpolation, so a crafted input cannot become shell code.
- Prefer `CLAUDE_REFRESH_TOKEN` over `CLAUDE_ACCESS_TOKEN` in CI: the access token
  expires within hours, and storing it means re-rotating the secret constantly.
- GitHub Secrets are masked in logs, but the **step summary is not a log** — it contains
  quota percentages, never credentials.
- **On a public repository, run logs, step summaries and artifacts are world-readable.**
  Secrets stay encrypted and `workflow_dispatch` requires write access, so credentials
  are safe — but your consumption figures are published. A step summary cannot be
  deleted on its own, so the workflow's final step deletes this workflow's *older* runs,
  bounding what is exposed to the single most recent snapshot. The current run must
  survive for you to read it, so the floor is one snapshot, not zero. Artifact retention
  is 1 day. Keep the repository private if even one snapshot is too much.
- The GCP service account needs only two read-only roles. Do not reuse a key that has
  write access to your project.

---

## Development

```bash
pip install -r requirements.txt
pip install pytest ruff

python -m pytest tests/ -q                 # 31 offline tests, no network
python tests/test_check_usage.py           # same suite without pytest installed
python -m ruff check --line-length 100 check_usage.py extract_tokens.py tests/
```

The tests replace the HTTP client with a fixture router, so they run offline and cover
both known Claude payload shapes, the Codex billing→headers fallback, Antigravity tier
thresholds, and each degradation path (expired token, HTML body, malformed service
account JSON, unrecognised payload).

To debug a live provider:

```bash
python check_usage.py --only claude -v          # log requests
python check_usage.py --only claude --json --raw # see the exact payload received
```

`--raw` is the fastest way to diagnose a `PARTIAL` result: it prints the provider's
untouched JSON so you can see which field names actually came back.

---

## Troubleshooting

**`[Claude Code] Status: UNCONFIGURED (Missing CLAUDE_REFRESH_TOKEN and no ~/.claude/.credentials.json)`**
Run `claude` once and log in, then re-run `extract_tokens.py`. On macOS the credentials
may live in the Keychain rather than a file — the resolver checks both.

**`[Claude Code] Status: ERROR (HTTP 400: refresh token expired)`**
Refresh tokens rotate on use. Re-run `extract_tokens.py --reveal` and update the
`CLAUDE_REFRESH_TOKEN` secret with the current value. See [Token safety](#token-safety).

**`Status: UNCONFIGURED (access token in the macOS Keychain has expired ...)`**
Working as designed — refreshing it here would log your `claude` CLI out. Let the CLI
refresh its own token, then retry:

```bash
claude -p "ok" && python3 check_usage.py --only claude
```

**`claude` itself says `OAuth session expired and could not be refreshed`**
Your refresh token was consumed without the replacement being stored. Re-authenticate
with `claude`. Versions of this tool before the Keychain refusal could cause this; it
cannot happen now unless you pass `--allow-refresh`.

**`[Claude Code] Status: PARTIAL (usage payload had no recognisable 5h/7d window)`**
The endpoint returned data in a layout the parser does not know. Run
`python check_usage.py --only claude --json --raw` to see the field names and open an
issue with that output (redact any tokens) — new aliases are a one-line addition to
`ClaudeResolver`.

**`[OpenAI Codex] Note: billing limits not exposed to this key`**
Expected for most API keys — `/dashboard/billing/*` is not available to all credential
classes. The rolling request allowance from the rate-limit headers is still reported. For
spend figures, check the platform dashboard or use a key created by the account owner.

**`[Antigravity] Status: UNCONFIGURED (google-auth is unavailable …)`**
Either `pip install -r requirements.txt`, or accept the fallback: the message also
appears when `cryptography` is half-installed. `python -c "import google.auth"` will
tell you which.

**`[Antigravity] Quota: n/a` with `Note: Cloud Monitoring unavailable or not permitted`**
The service account is missing `roles/monitoring.viewer`, or the project has recorded no
Vertex AI quota traffic in the look-back window. Widen it with `AGY_LOOKBACK_HOURS=168`.

**The workflow succeeds but the summary is empty**
Check the "Collect quota snapshot" step log. If it shows all three providers as
`UNCONFIGURED`, the secrets are not visible to the run — confirm they are *repository*
secrets (not environment secrets scoped elsewhere) and that the names match exactly.

---

## License

No license has been chosen for this repository yet. Add one (`LICENSE` in the root) if
you intend others to reuse the code.
