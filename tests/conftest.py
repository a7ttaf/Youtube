from pathlib import Path
import os
import sys

import pytest


BACKEND_PATH = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_PATH))

os.environ["UMS_TRUSTED_GATEWAY_TOKEN"] = "pytest-trusted-gateway-token"


@pytest.fixture(autouse=True)
def reset_app_settings_cache():
    from ums_smart_revenue.config.settings import load_app_settings

    load_app_settings.cache_clear()
    yield
    load_app_settings.cache_clear()

