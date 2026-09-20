"""Load each Lambda's `lambda_function` module under a distinct name.

The directories are deployment bundles, not packages: both contain a module
called `lambda_function`, and neither is importable by plain `import`.
"""

import importlib.util
import pathlib
import sys
import types

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def function_dirs() -> list[pathlib.Path]:
    """Every Lambda directory, i.e. every directory holding a lambda_function.py."""
    return sorted(p.parent for p in REPO_ROOT.glob("*/lambda_function.py"))


def load(function_name: str) -> types.ModuleType:
    """Import <function_name>/lambda_function.py as `<function_name>.lambda_function`."""
    alias = f"{function_name.replace('-', '_')}_lambda_function"
    if alias in sys.modules:
        return sys.modules[alias]

    path = REPO_ROOT / function_name / "lambda_function.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module
