"""Shared test setup for the Stock Evaluator suite.

CRITICAL ORDERING: src/market_screener.py has import-time side effects —
it sys.exit(1)s without SIMFIN_API_KEY, loads config/*.yaml, and applies
factor-decay to POTENTIAL_WEIGHTS at import. Environment must therefore
be pinned BEFORE the module is imported, which is why the env block runs
at conftest module level, not inside a fixture.

sf.set_api_key() only stores the string — no network call — so a dummy
key is safe when tests never trigger a SimFin download (they don't; all
scoring tests run from frozen fixtures).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# ---- Pin environment before ANY import of src.market_screener ---------------
os.environ.setdefault("SIMFIN_API_KEY", "test-dummy-key")
# Determinism knobs: match the defaults the golden master was built under.
os.environ["RESAMPLE_SEED"] = os.environ.get("RESAMPLE_SEED", "42")
os.environ["N_RESAMPLES"] = os.environ.get("N_RESAMPLES", "1000")
os.environ["DECAY_ENABLED"] = os.environ.get("DECAY_ENABLED", "1")

sys.path.insert(0, str(REPO))


@pytest.fixture(scope="session")
def ms():
    """The market_screener module, imported once with pinned env."""
    import src.market_screener as _ms
    return _ms


@pytest.fixture(scope="session")
def frozen_inputs():
    """Frozen stocks-table snapshot (inputs to the scorer)."""
    import pandas as pd
    p = FIXTURES / "frozen_stocks.pkl"
    if not p.exists():
        pytest.skip(
            "No golden master yet — run `python tests/build_golden_master.py` "
            "on a verified build first, then commit tests/fixtures/."
        )
    return pd.read_pickle(p)


@pytest.fixture(scope="session")
def golden():
    """Golden-master expected outputs + freeze metadata."""
    import json
    p = FIXTURES / "golden_scores.json"
    if not p.exists():
        pytest.skip(
            "No golden master yet — run `python tests/build_golden_master.py`."
        )
    return json.loads(p.read_text())
