import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import pytest


@pytest.fixture(scope="session")
def project_taxonomy():
    """The real industries.yaml shipped with the project."""
    from scout.industries import load_taxonomy

    return load_taxonomy(ROOT / "industries.yaml")


@pytest.fixture(scope="session")
def project_index(project_taxonomy):
    from scout.matcher import build_index

    return build_index(project_taxonomy)
