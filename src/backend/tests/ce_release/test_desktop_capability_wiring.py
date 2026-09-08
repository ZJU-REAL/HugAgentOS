"""The generated desktop CE payload must retain capability routes and ORM tables."""


def test_generated_ce_exports_all_device_capability_models():
    import core.db.models as models
    from core.db.engine import Base

    expected = {
        "DeviceCapabilityInstallation": "device_capability_installations",
        "DeviceCapabilityComponent": "device_capability_components",
        "DeviceCapabilityNamePreference": "device_capability_name_preferences",
        "DeviceCapabilityTransaction": "device_capability_transactions",
    }
    for name, table in expected.items():
        model = getattr(models, name)
        assert name in models.__all__
        assert model.__table__.name == table
        assert Base.metadata.tables[table] is model.__table__


def test_generated_ce_registers_cloud_bridge_and_device_management_routes():
    import importlib.util
    from api.app import app
    from api.routes.v1 import CE_ROUTERS, EE_ROUTERS

    assert importlib.util.find_spec("edition_ee") is None
    assert EE_ROUTERS == ()
    assert ("desktop_capability", "router") in CE_ROUTERS
    assert ("desktop_capabilities", "router") in CE_ROUTERS
    operations = {"get", "post", "put", "patch", "delete"}
    actual = {
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        for method in item
        if method in operations
    }
    assert {
        ("POST", "/v1/desktop/capability/cloud-bridge"),
        ("DELETE", "/v1/desktop/capability/cloud-bridge"),
        ("POST", "/v1/desktop/capability/token"),
        ("GET", "/v1/desktop/capability/skills/manifest"),
        ("GET", "/v1/desktop/capabilities/installations"),
        ("POST", "/v1/desktop/capabilities/sync"),
        ("POST", "/v1/desktop/capabilities/views/rebuild"),
        ("GET", "/v1/desktop/capabilities/mcp-json"),
    } <= actual
