## What changes

<!-- One or two sentences: what and why. -->

## Functions affected

<!-- The Lambda directories that will deploy on merge. Note that any changed
     file inside a function directory, deploy.json included, selects it.
     "None" if the PR touches no function directory. -->

## Checked

- [ ] `ruff check .` and `pytest` pass locally
- [ ] New logic is covered by tests
- [ ] New dependencies are pinned in the right function's `requirements.txt`
- [ ] No new error text can leak the bot token (`redact()`)

## Manual steps after merge

<!-- The deploy is automatic, but not everything is: a new LAMBDA_ENV_<NAME>
     secret, a configuration change in the AWS console (deploy.json no longer
     affects an existing function), reading the log after the first invoke.
     "None" if there are none. -->
