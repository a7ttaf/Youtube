from pathlib import Path
import os
import sys


BACKEND_PATH = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_PATH))

os.environ.setdefault("UMS_TRUSTED_GATEWAY_TOKEN", "pytest-trusted-gateway-token")

