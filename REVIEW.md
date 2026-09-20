# Review checklist

Review rules for this repository, read by `.github/workflows/claude-review.yml`.
Change the rules here, not in the workflow prompt. `CLAUDE.md` describes how
the repository is laid out.

Look for what `ruff` and `pytest` cannot catch. Do not repeat them.

## Language

Everything written into this repository is in English: code, comments, commit
messages, workflow files, documentation and review output. The only exception
is user-facing Telegram strings, which stay Russian. Flag any new Russian text
outside those strings as blocking.

## Blocking

**Secrets.** The Telegram bot token must never reach a response or a log.
`requests` puts the full URL into `HTTPError` text, and the Telegram URL
contains the token, so any new error text that leaves the Lambda goes through
`redact()`. The same holds for `MCP_AUTH_TOKEN`: it must not be printed, from
either the `Authorization` header or the query string.

**Nothing about the configuration reaches an unauthenticated caller.** The
Function URL is `AuthType: NONE`, so the check in the code is the only thing
guarding it. Until a request is authorized, the response must not name
environment variables, reasons for a 503, or anything else about how the
server is set up. The reason goes to CloudWatch.

**Auth fails closed.** An unset `MCP_AUTH_TOKEN` is a 503, never "open to
everyone". Tokens are compared with `hmac.compare_digest`.

**The Shopify feed is untyped.** Every new field read from it goes through
`.get()` and `isinstance`. A malformed product is skipped, not allowed to
crash the run.

**Dependencies.** There are no layers in this account, so everything ships
inside the zip. A new dependency is a pinned (`==`) line in that function's
`requirements.txt`. Nothing is vendored into git.

**`deploy.json` is the source of truth** for runtime and architecture. Do not
hardcode either in a workflow.

**A new function** is a directory with `lambda_function.py`,
`requirements.txt` and `deploy.json`. It needs no `deploy.yml` edits — if a PR
proposes some, ask why. The directory name must equal the AWS function name.

**Tests.** New logic needs new tests in `tests/`. The pure functions — feed
parsing, Markdown rendering, JSON-RPC dispatch — are testable without network.

## Check carefully

**Telegram.** Only Telegram can validate the markup. If a PR touches
`markdown_to_telegram_html`, claims that "Telegram accepts this" must rest on
the live API, not the docs. The message ceiling is 4096 characters; `defa-luci`
does not chunk and will fail on a large restock.

**Partial delivery.** A failing chat id must not abort the loop over the rest.
`telegram-mcp` does this; `defa-luci` does not yet.

**Tool failures are results, not protocol errors.** In `telegram-mcp` a
Telegram outage returns `isError: true` inside a 200. JSON-RPC error codes are
reserved for malformed or unknown requests.

**MCP 2026-07-28 obligations.** There is no SDK in the bundle, so these are met
by hand: `server/discover` must answer, every result carries `resultType`, list
results carry `ttlMs` and `cacheScope`, and the server identifies itself in
`_meta["io.modelcontextprotocol/serverInfo"]`.

**Timeouts.** A request timeout must fit inside the Lambda timeout with room to
spare, counting every request one invocation can make.

**The environment secret replaces the whole map.** `LAMBDA_ENV_<NAME>` is the
single source of truth: removing a variable means removing it from the JSON,
and console edits are wiped by the next deploy.

**Never use `toJSON(secrets)`** — GitHub holds such runs as potentially
malicious.
