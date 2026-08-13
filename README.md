# deepseek-harness-usage

DeepSeek Harness account balance monitor. It follows the same usage-tool
interface as the other `/code/*usage` packages, but reports the allowance
DeepSeek actually exposes for API accounts: current prepaid balance and API
availability.

It does not estimate account usage from local Harness sessions.

## Example output

These values are synthetic.

```text
DeepSeek Harness balance
Status: live
API access: available
USD: 123.45 total (23.45 granted + 100.00 topped up)
```

Statusline:

```text
dsh:bal:USD123.45
```

## Install

```bash
uv tool install deepseek-harness-usage
```

For local development:

```bash
uv tool install .
```

## Commands

| Command | Description |
| --- | --- |
| `deepseek-harness-usage` | Show a live account balance |
| `deepseek-harness-usage status` | Same as above |
| `deepseek-harness-usage json` | Print normalized live JSON |
| `deepseek-harness-usage statusline` | Print compact cache-first output |
| `deepseek-harness-usage refresh` | Refresh the cache and print the balance |
| `deepseek-harness-usage daemon [-i SECS]` | Keep the cache fresh |
| `deepseek-harness-usage install` | Print installation instructions |

Options:

- `--max-age SECS` sets the statusline cache lifetime. The default is 300.
- `--refresh` bypasses a fresh statusline cache.
- `-i SECS` sets the daemon refresh interval. The default is 300.

## Data source

The tool calls DeepSeek's supported
[`GET /user/balance`](https://api-docs.deepseek.com/api/get-user-balance)
endpoint. The response supplies API availability and total, granted, and
topped-up balances per currency. Monetary values stay decimal strings from the
provider and are never converted through binary floating point.

DeepSeek does not document an API for aggregate token history, amount spent,
per-key usage, quota percentages, or reset windows. Its API is billed by token
usage against account balance, and concurrency limits are not a consumable
quota. The package therefore reports balance without inventing a percentage or
reset time.

## Authentication

Every live request rereads the key. A dedicated
`DEEPSEEK_HARNESS_USAGE_API_KEY` is the highest-priority source and explicitly
identifies a key for the official DeepSeek account. Without it, the tool uses
the default DeepSeek Harness credential order:

1. The inherited process environment.
2. `$DSH_HOME/.credentials.yaml`.
3. The invoking directory's `.env`.
4. `$DSH_HOME/.env`.

`DSH_HOME` defaults to `~/.dsh`. The tool never refreshes, rotates, rewrites,
prints, or caches the key. It sends the key only to the fixed HTTPS balance
endpoint at `api.deepseek.com`, and rejects redirects before the authorization
header can be forwarded. Like Harness, it refuses a managed credential file
that is readable by group or other users. It parses and validates the complete
YAML document, so an invalid sibling entry cannot be silently ignored.

A Harness deployment that configures a different credential reference or a
custom credential-provider path cannot be discovered through the current CLI.
Set `DEEPSEEK_HARNESS_USAGE_API_KEY` to the official DeepSeek account key when
running this tool.

The balance endpoint accepts DeepSeek-issued API keys. If the Harness route
uses a gateway or another compatible provider, do not send that provider's key
to this tool. The package refuses custom endpoints and credential references
it can identify in the environment or Harness settings before reading the
credential. Profile-specific plugin patches are not discoverable from a
standalone invocation, so use the dedicated variable for those deployments.

## Cache

The credential-free normalized snapshot is written atomically to
`$DSH_HOME/usage-limits.json`, or `~/.dsh/usage-limits.json` when `DSH_HOME` is
unset. `DEEPSEEK_HARNESS_USAGE_FILE` overrides that path.

The cache contains sensitive account metadata even though it contains no raw
credential. New cache files use mode `0600`, newly created parent directories
use mode `0700`, and replacement is atomic and does not follow a final cache
symlink. Do not publish or share the cache.

Freshness states are explicit:

| Status | Meaning |
| --- | --- |
| `live` | Returned by the balance API now |
| `cached` | Read from a cache younger than `--max-age` |
| `stale` | A live refresh failed, so an older valid cache was returned |
| `unavailable` | No live result or valid cache is available |

Cache entries are bound to a one-way SHA-256 fingerprint of the key that
produced them. A key rotation or per-run override cannot reuse another
account's cached balance. A failed statusline refresh records a 30-second
backoff so repeated prompts do not repeatedly wait on an unavailable service.
Refreshes use a non-blocking OS advisory lock, which is released automatically
if a writer exits or crashes.

Schema version 1 has this form:

```json
{
  "schema_version": 1,
  "provider": "deepseek",
  "client": "deepseek_harness",
  "source": "deepseek_user_balance_api",
  "retrieved_at": "2026-08-13T20:00:00+00:00",
  "last_attempt_at": "2026-08-13T20:00:00+00:00",
  "credential_fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "status": "live",
  "account_balance": {
    "available": true,
    "balances": [
      {
        "currency": "USD",
        "total": "123.45",
        "granted": "23.45",
        "topped_up": "100.00"
      }
    ]
  }
}
```

## Reliability boundary

Balance is account-wide and authoritative for the account behind the API key.
It can change because of other clients, other keys, top-ups, or grant expiry.
Those changes make balance deltas unsuitable as exact spend history.

Harness does persist provider-reported tokens for ordinary assistant calls, but
that is not a complete billing ledger. Existing projections can omit retried
attempts, compaction, session-title calls, disconnected calls, and every use of
the key outside that Harness installation. Summing those records would produce
a precise-looking but incomplete account total.

A future local breakdown should be a separate Harness plugin wrapping the
documented `llm/stream` waterfall and recording each returned usage chunk
before forwarding it. That would reliably cover instrumented Harness calls
going forward, but it would still be local accounting rather than account-wide
usage.

## Development

```bash
uv --no-config lock --check
uv --no-config run --locked python -m unittest discover -s tests
uv --no-config build --no-sources
```
