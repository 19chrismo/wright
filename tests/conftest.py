import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "wright_home"
    monkeypatch.setenv("WRIGHT_HOME", str(h))
    return h
