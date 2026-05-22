"""Run the UMS Smart Revenue local validation gate from a source checkout."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from ums_smart_revenue.devtools.quality_gate import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
