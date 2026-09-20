"""Rules every Lambda directory must satisfy.

A directory is the unit of deploy and the workflow needs no edits to pick up a
new one — which also means a new directory gets no review from the workflow
itself. These checks are that review.
"""

import json
import re

import pytest

from conftest import function_dirs, load

FUNCTIONS = function_dirs()
NAMES = [path.name for path in FUNCTIONS]

# The code uses `str | None` and builtin generics.
MIN_PYTHON = (3, 10)
REQUIRED_DEPLOY_KEYS = {"runtime", "handler", "role", "architecture", "timeout", "memory_size"}
PINNED_REQUIREMENT = re.compile(r"^[A-Za-z0-9._-]+(\[[^\]]+\])?==[^\s#]+$")


def test_the_repository_has_functions():
    assert FUNCTIONS, "no <name>/lambda_function.py found"


@pytest.mark.parametrize("name", NAMES)
def test_the_directory_name_is_a_valid_lambda_function_name(name):
    # The workflow deploys to a function named after the directory.
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name), name


@pytest.mark.parametrize("name", NAMES)
def test_the_entrypoint_aws_expects_exists(name):
    handler = getattr(load(name), "lambda_handler", None)
    assert callable(handler), f"{name}/lambda_function.py has no lambda_handler"


@pytest.mark.parametrize("name", NAMES)
def test_deploy_json_exists_and_is_complete(name):
    # Without it the workflow cannot create the function, and the job fails
    # rather than skipping quietly. Missing it is only invisible while the
    # function still exists in AWS.
    config_path = next(path for path in FUNCTIONS if path.name == name) / "deploy.json"
    assert config_path.is_file(), f"{name}/deploy.json is missing"

    config = json.loads(config_path.read_text())
    assert config.keys() >= REQUIRED_DEPLOY_KEYS, REQUIRED_DEPLOY_KEYS - config.keys()
    assert config["handler"] == "lambda_function.lambda_handler"
    assert config["role"].startswith("arn:aws:iam::"), config["role"]
    assert config["architecture"] in {"x86_64", "arm64"}
    assert isinstance(config["timeout"], int) and 1 <= config["timeout"] <= 900
    assert isinstance(config["memory_size"], int) and config["memory_size"] >= 128


@pytest.mark.parametrize("name", NAMES)
def test_the_runtime_is_new_enough_for_the_syntax_used(name):
    config_path = next(path for path in FUNCTIONS if path.name == name) / "deploy.json"
    runtime = json.loads(config_path.read_text())["runtime"]
    match = re.fullmatch(r"python(\d+)\.(\d+)", runtime)
    assert match, f"{name}: unexpected runtime {runtime!r}"
    assert (int(match.group(1)), int(match.group(2))) >= MIN_PYTHON, runtime


@pytest.mark.parametrize("name", NAMES)
def test_the_request_timeout_fits_inside_the_lambda_timeout(name):
    # A request timeout longer than the Lambda timeout never fires: the runtime
    # kills the invocation first and the error says "task timed out" instead.
    directory = next(path for path in FUNCTIONS if path.name == name)
    config = json.loads((directory / "deploy.json").read_text())
    request_timeout = getattr(load(name), "TIMEOUT_SECONDS", None)
    if request_timeout is None:
        pytest.skip(f"{name} declares no TIMEOUT_SECONDS")
    assert request_timeout < config["timeout"], (
        f"{name}: TIMEOUT_SECONDS={request_timeout} >= Lambda timeout {config['timeout']}"
    )


@pytest.mark.parametrize("name", NAMES)
def test_every_dependency_is_pinned(name):
    # Nothing is vendored: the zip is built by pip at deploy time, so an
    # unpinned line means the bundle changes without a commit.
    directory = next(path for path in FUNCTIONS if path.name == name)
    requirements = directory / "requirements.txt"
    if not requirements.is_file():
        pytest.skip(f"{name} has no dependencies")

    for line in requirements.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            assert PINNED_REQUIREMENT.fullmatch(line), f"{name}: unpinned requirement {line!r}"


@pytest.mark.parametrize("name", NAMES)
def test_no_secret_is_committed_next_to_the_code(name):
    # Environment comes from the LAMBDA_ENV_<NAME> secret, never from the repo.
    directory = next(path for path in FUNCTIONS if path.name == name)
    for path in directory.rglob("*"):
        assert path.name not in {".env", ".env.local"}, path
    source = (directory / "lambda_function.py").read_text()
    assert not re.search(r"\d{8,10}:[A-Za-z0-9_-]{35}", source), (
        f"{name}: what looks like a Telegram bot token is hard-coded"
    )
