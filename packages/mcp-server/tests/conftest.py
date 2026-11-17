from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SDK_TESTS = Path(__file__).resolve().parents[2] / "buyer-sdk" / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("sdk_helpers", SDK_TESTS)
sdk_helpers = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["sdk_helpers"] = sdk_helpers
_spec.loader.exec_module(sdk_helpers)  # type: ignore[union-attr]
