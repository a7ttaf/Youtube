"""Run the UMS Smart Revenue strict test gate from a source checkout."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from ums_smart_revenue.devtools.quality_gate import (  # noqa: E402
    build_test_gate_commands,
    run_gate,
)

if __name__ == "__main__":
    raise SystemExit(run_gate(commands=build_test_gate_commands()))
