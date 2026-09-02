# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

One directory per AWS Lambda function, each a self-contained deployment bundle. There is no shared package, no build system, and no test suite — a directory is the unit of deploy.

- `defa-luci-bot/` — polls the `dolls` collection on `defalucy.com` (Shopify) and pushes in-stock dolls to Telegram.

A function directory contains `lambda_function.py` (with the `lambda_handler` entrypoint AWS expects) plus any pure-Python dependencies vendored flat alongside it, so the directory can be zipped and uploaded as-is. Nothing is vendored right now: `requests` comes from a Lambda layer or the runtime. When adding a dependency, decide deliberately — vendor it into the function directory, or add it to the layer. A vendored file is not project code; never edit or refactor one.

Runtime must be Python 3.10+ — the code uses `str | None` and builtin generics.

## Deploy

Deploys are automatic: pushing or merging to `main` triggers `.github/workflows/deploy.yml`.

The workflow diffs the push against its base, deploys only the directories that changed, and runs one matrix job per function with `fail-fast: false`. **The directory name must equal the Lambda function name in AWS.** Adding a new function means adding a directory with a `lambda_function.py` in it — the workflow needs no edits. `workflow_dispatch` allows a manual run, optionally scoped to a space-separated list of function names.

AWS auth is OIDC: secret `AWS_ROLE_ARN` plus repository variable `AWS_REGION`. The IAM role needs `lambda:UpdateFunctionCode`, `lambda:UpdateFunctionConfiguration` and `lambda:GetFunction`. Setup steps are documented in the workflow's header comment.

### Secrets and environment variables

A function's environment variables come from one GitHub secret named `LAMBDA_ENV_<FUNCTION_NAME>` — uppercased, hyphens replaced by underscores (`defa-luci-bot` → `LAMBDA_ENV_DEFA_LUCI_BOT`). Its value is a JSON object of variable names to values, and the workflow writes it with `update-function-configuration`.

That call **replaces the whole environment map**, so the secret is the single source of truth: anything set by hand in the AWS console is wiped on the next deploy, and removing a variable means removing it from the JSON. A function with no such secret keeps whatever environment it already has.

`defa-luci-bot` expects `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_IDS` (comma-separated).

## defa-luci-bot design

The function reads the Shopify storefront JSON feed at `/collections/dolls/products.json` rather than parsing the rendered page, so it does not depend on theme markup. `fetch_collection_products` pages through the feed until it gets a page shorter than `PAGE_LIMIT`.

`extract_available_products` keeps a product when any variant has `available is True`, and reports the cheapest available variant price. Every field is read defensively (`isinstance` checks, `.get`) because the feed is third-party and untyped.

Then, only if something is in stock, `build_telegram_message` renders the list and `send_telegram_messages` posts it to every chat id.

Things worth knowing before changing this:

- **Silent empty results are the expected failure mode.** If the collection handle changes or the store stops exposing `products.json`, the handler returns `available_count: 0` with a 200, not an error. Verify against the live feed rather than trusting a green run.
- **The function has no memory.** Every invocation notifies about everything currently in stock, so it will re-send the same dolls on every schedule tick. Any deduplication would need external state (DynamoDB, S3) that does not exist yet.
- **Never let the bot token reach a response body.** `requests` embeds the full URL in `HTTPError` messages, and the Telegram API URL contains the token. `redact()` exists for exactly this; route any new error text through it.
- **The message is not chunked.** Telegram rejects messages over 4096 characters, so a large restock currently fails the send and returns a 502.

`lambda_handler` maps failures to status codes: `HTTPError` → 502, other `RequestException` → 502, anything else (including missing configuration) → 500. All responses are built by `json_response` and serialized with `ensure_ascii=False`.

Code comments are in English. User-facing Telegram strings are in Russian — keep them that way.
