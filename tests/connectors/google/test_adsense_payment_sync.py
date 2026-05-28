def test_connector_key_constant_is_canonical() -> None:
    from ums_smart_revenue.connectors.google.registry import (
        ADSENSE_MANAGEMENT_CONNECTOR_KEY,
    )

    assert ADSENSE_MANAGEMENT_CONNECTOR_KEY == "adsense-management"


def test_resolve_connector_credentials_is_public() -> None:
    from ums_smart_revenue.connectors.runs.orchestrator import (
        resolve_connector_credentials,
    )

    assert callable(resolve_connector_credentials)
