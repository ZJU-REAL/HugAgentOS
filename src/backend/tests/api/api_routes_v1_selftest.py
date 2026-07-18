#!/usr/bin/env python
"""Self-test for v1 API routes.

Tests that all v1 API routes are properly registered and can be imported.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


def test_routes_registration():
    """Test that all v1 routes are registered."""
    from api.app import app

    # Get all v1 routes
    v1_routes = []
    for route in app.routes:
        if hasattr(route, 'path') and '/v1/' in route.path:
            v1_routes.append(route.path)

    expected_routes = [
        # Chats
        '/v1/chats',
        '/v1/chats/search',
        '/v1/chats/{chat_id}',
        '/v1/chats/{chat_id}/messages',
        # Users
        '/v1/me',
        '/v1/users/{user_id}/preferences',
        # Catalog
        '/v1/catalog',
        '/v1/catalog/{kind}/{id}',
        # Knowledge Base
        '/v1/catalog/kb',
        '/v1/catalog/kb/{kb_id}/documents',
        # Personal system settings / personal logs (delegated to CE)
        '/v1/me/system/access',
        '/v1/me/system/service-configs',
        '/v1/me/logs/tools',
        '/v1/me/logs/usage',
        # Model management (must still be registered after switching the gate to require_system_settings)
        '/v1/models/providers',
        '/v1/models/roles',
    ]

    print("Testing v1 API routes registration...")
    print("=" * 60)

    missing_routes = []
    for expected in expected_routes:
        if expected in v1_routes:
            print(f"✓ {expected}")
        else:
            print(f"✗ {expected} - NOT FOUND")
            missing_routes.append(expected)

    print("=" * 60)
    print(f"Total expected routes: {len(expected_routes)}")
    print(f"Total registered v1 routes: {len(v1_routes)}")
    print(f"Missing routes: {len(missing_routes)}")

    if missing_routes:
        print("\nMissing routes:")
        for route in missing_routes:
            print(f"  - {route}")
        return False

    print("\n✓ All v1 routes registered successfully!")
    return True


def test_route_imports():
    """Test that all route modules can be imported."""
    print("\nTesting route module imports...")
    print("=" * 60)

    modules = [
        ('api.routes.v1.chats', 'router'),
        ('api.routes.v1.users', 'router'),
        ('api.routes.v1.catalog', 'router'),
        ('api.routes.v1.kb', 'router'),
    ]

    all_ok = True
    for module_name, router_name in modules:
        try:
            module = __import__(module_name, fromlist=[router_name])
            router = getattr(module, router_name, None)
            if router:
                print(f"✓ {module_name}.{router_name}")
            else:
                print(f"✗ {module_name}.{router_name} - Router not found")
                all_ok = False
        except Exception as e:
            print(f"✗ {module_name}.{router_name} - Error: {e}")
            all_ok = False

    print("=" * 60)
    if all_ok:
        print("✓ All route modules imported successfully!")
    else:
        print("✗ Some route modules failed to import")

    return all_ok


def test_exception_handler():
    """Test that exception handler is registered."""
    print("\nTesting exception handler registration...")
    print("=" * 60)

    from api.app import app
    from core.infra.exceptions import AppException

    # Check if exception handler is registered
    handlers = app.exception_handlers
    if AppException in handlers:
        print("✓ AppException handler registered")
        return True
    else:
        print("✗ AppException handler not registered")
        return False


def main():
    """Run all self-tests."""
    print("V1 API Routes Self-Test")
    print("=" * 60)
    print()

    results = []

    # Test routes registration
    results.append(("Routes Registration", test_routes_registration()))

    # Test route imports
    results.append(("Route Imports", test_route_imports()))

    # Test exception handler
    results.append(("Exception Handler", test_exception_handler()))

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    all_passed = True
    for test_name, passed in results:
        status = "PASS" if passed else "FAIL"
        symbol = "✓" if passed else "✗"
        print(f"{symbol} {test_name}: {status}")
        if not passed:
            all_passed = False

    print("=" * 60)

    if all_passed:
        print("\n✓ All tests passed!")
        return 0
    else:
        print("\n✗ Some tests failed!")
        return 1


if __name__ == "__main__":
    sys.exit(main())
