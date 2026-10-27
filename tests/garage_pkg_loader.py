"""Load the real package so relative imports resolve, without Home Assistant.

``garage_door`` imports ``.door_evidence``, so it cannot be loaded as a standalone
file. Building a minimal package namespace keeps the production import graph
intact instead of stubbing it -- a stub would let the test pass on an import
shape that does not exist in production.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "roborock_plus"
ALIAS = "_garage_pkg"


def load_package():
    if ALIAS in sys.modules:
        return sys.modules[ALIAS]
    spec = importlib.util.spec_from_file_location(
        ALIAS, PACKAGE / "__init__.py",
        submodule_search_locations=[str(PACKAGE)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[ALIAS] = module
    # __init__ pulls in Home Assistant; only the namespace is needed here, so the
    # module body is never executed.
    return module


load_package()
