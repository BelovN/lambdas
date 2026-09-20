# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

One directory per AWS Lambda function, each a self-contained deployment bundle. There is no shared package and no build system — a directory is the unit of deploy.

- `defa-luci/` — polls the `dolls` collection on `defalucy.com` (Shopify) and pushes in-stock dolls to Telegram.
- `telegram-mcp/` — stateless MCP server behind a Lambda Function URL, exposing one `send_notification` tool that relays a message to Telegram.
- `tests/` — pytest suite covering the pure logic of every function plus the repository conventions below. It is never deployed: the deploy workflow only picks up directories holding a `lambda_function.py`.
- `REVIEW.md` — the review checklist the PR-review workflow follows, including the English-only rule above.

A function directory contains `lambda_function.py` (with the `lambda_handler` entrypoint AWS expects), an optional `requirements.txt`, and a `deploy.json`. **There are no Lambda layers in this account** — every dependency has to end up inside the zip, and the deploy workflow puts it there by running `pip install --target` into the build directory. Adding a dependency means adding a pinned line to that function's `requirements.txt`; nothing is vendored into git.

Runtime must be Python 3.10+ — the code uses `str | None` and builtin generics. Both functions run `python3.13` on `x86_64`.

The build Python must match the Lambda runtime. `pip` resolves interpreter-specific wheels — `charset_normalizer`, pulled in by `requests`, ships a `cp313` binary wheel — so building on a different Python produces a bundle that fails at import time with `Runtime.ImportModuleError`. Both workflows therefore read `runtime` and `architecture` out of each function's `deploy.json` and feed them to `actions/setup-python` and `pip`; nothing is hardcoded. An `arm64` function is built with explicit `--platform manylinux2014_aarch64 --only-binary=:all:` because the runner is `x86_64`.

## Checks

`.github/workflows/ci.yml` runs on every pull request and needs no AWS credentials:

- `ruff check .` — config in `pyproject.toml`, line length 100. Formatting is not enforced.
- `pytest` — `tests/`, no network. `tests/conftest.py` loads each `lambda_function.py` under a distinct module name, since the directories are bundles rather than packages.
- One bundle job per function: it builds the zip exactly as the deploy does, then imports `lambda_function` on the function's own Python. This is what closes the documented failure mode below, where a bundle missing a dependency deploys green and dies on cold start.

`tests/test_repo_conventions.py` enforces the rules a new directory has to satisfy — a valid function name, a callable `lambda_handler`, a complete `deploy.json`, a runtime new enough for the syntax, pinned requirements, a request timeout that fits inside the Lambda timeout, and no committed secret. A new Lambda still needs no workflow edits; it does need to satisfy these.

Run the same checks locally with `pip install -r requirements-dev.txt && ruff check . && pytest`.

## Deploy

Deploys are automatic: pushing or merging to `main` triggers `.github/workflows/deploy.yml`.

The workflow diffs the push against its base, deploys only the directories that changed, and runs one matrix job per function with `fail-fast: false`. Each job installs the function's `requirements.txt` into a staging copy, zips it, and uploads.

A function that already exists in AWS gets `update-function-code`. One that does not is created from `deploy.json` (`runtime`, `role`, `handler`, `architecture`, `timeout`, `memory_size`, and `function_url`), and a directory with no `deploy.json` fails the job rather than being skipped quietly. `deploy.json` is build metadata and is stripped from the bundle. Creation is a one-time path: editing `deploy.json` afterwards changes nothing, because the workflow never reconfigures an existing function — adjust it in AWS, or delete and let the next deploy recreate it.

Because of that, `deploy.json` is a record of *intended* state, not a mirror of AWS. `telegram-mcp/deploy.json` says `timeout: 30`; the live function was created with 15 and still has it. Reconcile in the console.

## Review and merge

Pull requests are the working unit — `main` is deployed on merge, so nothing lands on it unreviewed.

- `.github/workflows/ci.yml` — the checks above, on every PR.
- `.github/workflows/claude-review.yml` — reviews each PR against `REVIEW.md`. Edit the checklist there, not the workflow prompt.
- `.github/workflows/claude.yml` — responds to `@claude` in an issue, a PR comment or a review thread, and pushes fixes to the PR branch.

Both Claude workflows need the GitHub App installed and a `CLAUDE_CODE_OAUTH_TOKEN` secret — a subscription token from `claude setup-token`, not a purchased API key. Swap in `anthropic_api_key` if you would rather bill the API. Neither workflow passes `github_token`: GitHub does not run workflows on commits made with the default `GITHUB_TOKEN`, so passing it would mean CI never runs on the fixes Claude pushes. Without the secret the jobs fail; CI and deploy are unaffected.

Both existing functions currently share the execution role `defa-luci-role-iy4dhc6v`, which only grants CloudWatch Logs. It is named after one function but used by both; giving `telegram-mcp` its own role would be an improvement. **The directory name must equal the Lambda function name in AWS.** Adding a new function means adding a directory with a `lambda_function.py` in it — the workflow needs no edits. `workflow_dispatch` allows a manual run, optionally scoped to a space-separated list of function names.

AWS auth is OIDC: secret `AWS_ROLE_ARN` plus repository variable `AWS_REGION` (defaults to `eu-north-1`, where the functions live). The IAM role needs `lambda:UpdateFunctionCode`, `lambda:UpdateFunctionConfiguration` and `lambda:GetFunction`. Setup steps are documented in the workflow's header comment — note in particular that GitHub now issues subject claims containing numeric owner and repo ids (`repo:OWNER@ID/REPO@ID:ref:...`); a trust policy written against the old `repo:OWNER/REPO:ref:...` form fails with `AccessDenied`.

### Secrets and environment variables

A function's environment variables come from one GitHub secret named `LAMBDA_ENV_<FUNCTION_NAME>` — uppercased, hyphens replaced by underscores (`defa-luci` → `LAMBDA_ENV_DEFA_LUCI`). Its value is a JSON object of variable names to values, and the workflow writes it with `update-function-configuration`.

The workflow passes exactly one secret per job, addressed by a name the `detect` job computes into the matrix (`secrets[matrix.env_secret]`). Do not reach for `toJSON(secrets)`: dumping every secret into one variable is the canonical exfiltration pattern, and since July 2026 GitHub automatically holds workflow runs it reads as potentially malicious, blocking them on manual approval. That hold applies to public repositories, is on by default, and there is no setting to turn it off.

That call **replaces the whole environment map**, so the secret is the single source of truth: anything set by hand in the AWS console is wiped on the next deploy, and removing a variable means removing it from the JSON. A function with no such secret keeps whatever environment it already has.

`defa-luci` expects `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_IDS` (comma-separated).

## telegram-mcp design

A single Lambda Function URL speaks MCP's Streamable HTTP transport. The 2026-07-28 revision made the protocol core stateless — `initialize`/`initialized` and `Mcp-Session-Id` were retired — which is what makes one request per invocation viable with no session store. `dispatch` still answers `initialize` so clients on 2025-06-18 and 2025-03-26 connect, echoing back whichever version the client asked for when it is supported.

There is no MCP SDK in the bundle: the server implements the JSON-RPC surface it needs (`server/discover`, `initialize`, `ping`, `tools/list`, `tools/call`) directly, which is a few dozen lines against a dependency that assumes a long-lived process.

Hand-rolling means the 2026-07-28 obligations have to be met by hand. Servers **MUST** implement `server/discover` — it is what replaced the handshake, and a modern client calls it first; answering `-32601` there is what makes a connector report the server as unreachable. Every result carries `resultType: "complete"`, list results carry `ttlMs` and `cacheScope`, and results identify the server under `_meta["io.modelcontextprotocol/serverInfo"]`.

`send_notification` takes Markdown. `markdown_to_telegram_html` renders a practical subset into the small tag set Telegram's HTML parse mode accepts (`b`, `i`, `s`, `a`, `code`, `pre`, `blockquote`); headings become bold, bullets become `\u2022`, and horizontal rules become a line of dashes, because Telegram has no tags for them. Code spans and fenced blocks are lifted out before escaping so their contents are never treated as markup, and link targets are parked behind placeholders so emphasis rules cannot corrupt a URL.

Things worth knowing before changing this:

- **Only Telegram can validate the markup.** Every supported construct was checked against the live API rather than against the docs. If the HTML fails to parse, Telegram answers 400 and the send is retried once as plain text: a notification is worth more unformatted than lost.
- **Raw HTML in the message is escaped, not honoured.** `<b>` typed by a caller arrives as literal text, so a message can never inject markup.
- **Two ways to present the token, one secret.** `Authorization: Bearer` is checked first; `?k=<token>` is the fallback for clients that can only be given a URL — the claude.ai connector form takes a URL and OAuth fields, with nowhere to put a header. Prefer the header: a query string reaches access logs and referrers, a header does not. A present-but-wrong header is a failed attempt and does not fall through to the query parameter.
- **Auth fails closed.** An unset `MCP_AUTH_TOKEN` raises rather than defaulting to open, so a misconfigured deploy returns 503 instead of exposing a public endpoint that spams Telegram. The reason goes to CloudWatch, never to the caller — the endpoint is unauthenticated at that point, so it must not describe its own configuration. Keep it that way — the Function URL itself is `AuthType: NONE`, so this check is the only thing guarding it. Tokens are compared with `hmac.compare_digest`.
- **Partial delivery is reported as such.** A failing chat id no longer aborts the loop, so the result distinguishes "delivered to 2, 1 failed" from a total failure. Only a configuration error (missing token or chat ids) short-circuits before any send.
- **Tool failures are results, not protocol errors.** A Telegram outage returns `isError: true` inside a 200 so the model can see and report it; JSON-RPC error codes are reserved for malformed or unknown requests.
- **The bot token must never reach a response.** Same trap as `defa-luci`: `requests` embeds the URL in `HTTPError` messages. Route new error text through `redact()`.
- **A Function URL rewrites `WWW-Authenticate`.** It reaches the client as `x-amzn-Remapped-www-authenticate`, so the 401 carries no usable challenge. Any future OAuth support would need the discovery hint somewhere else, or API Gateway instead of a Function URL.
- **A public Function URL needs two permissions.** Since October 2025 Lambda requires both `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` (the latter conditioned on `lambda:InvokedViaFunctionUrl`) in the resource policy. With only the first, every request gets a 403 from Lambda itself, before the handler runs, even with `AuthType: NONE`.
- **Batching is rejected.** JSON-RPC batches were removed from MCP in 2025-06-18, so a top-level array gets `-32600`.

Environment: `MCP_AUTH_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS` (comma-separated).

## defa-luci design

The function reads the Shopify storefront JSON feed at `/collections/dolls/products.json` rather than parsing the rendered page, so it does not depend on theme markup. `fetch_collection_products` pages through the feed until it gets a page shorter than `PAGE_LIMIT`.

`extract_available_products` keeps a product when any variant has `available is True`, and reports the cheapest available variant price. Every field is read defensively (`isinstance` checks, `.get`) because the feed is third-party and untyped.

Then, only if something is in stock, `build_telegram_message` renders the list and `send_telegram_messages` posts it to every chat id.

Things worth knowing before changing this:

- **Silent empty results are the expected failure mode.** If the collection handle changes or the store stops exposing `products.json`, the handler returns `available_count: 0` with a 200, not an error. Verify against the live feed rather than trusting a green run.
- **The function has no memory.** Every invocation notifies about everything currently in stock, so it will re-send the same dolls on every schedule tick. Any deduplication would need external state (DynamoDB, S3) that does not exist yet.
- **Never let the bot token reach a response body.** `requests` embeds the full URL in `HTTPError` messages, and the Telegram API URL contains the token. `redact()` exists for exactly this; route any new error text through it.
- **A deploy that drops a dependency fails silently until invocation.** `update-function-code` succeeds regardless; the function then dies on cold start with `Runtime.ImportModuleError`. The CI bundle job and the deploy's own import check now catch this before the upload, but neither runs the handler — after changing the bundle's contents, invoke once and read the log rather than trusting a green workflow run.
- **The message is not chunked.** Telegram rejects messages over 4096 characters, so a large restock currently fails the send and returns a 502.
- **One failing chat id aborts the whole send.** Unlike `telegram-mcp`, which reports partial delivery, `send_telegram_messages` raises on the first failure: chats already messaged are reported as a total failure, and the next run re-sends to them. Pinned by `test_a_failing_chat_currently_aborts_the_whole_send`.

`lambda_handler` maps failures to status codes: `HTTPError` → 502, other `RequestException` → 502, anything else (including missing configuration) → 500. All responses are built by `json_response` and serialized with `ensure_ascii=False`.

Everything written into this repository is in English: code, comments, commit messages, workflow files, documentation and review output. The one exception is user-facing Telegram strings, which stay Russian — keep them that way.
