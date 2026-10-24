"""The rewriter changes ownership without executing or changing module logic."""
from scripts.refactor.rewrite_imports import rewrite

MOVES = {"core.services.old_service": "features.example.service"}


def apply(source, old="caller", moves=None, **kwargs):
    return rewrite(source, old, moves or MOVES, **kwargs)


def test_function_local_import_keeps_alias():
    result, review = apply("def run():\n    from core.services.old_service import get as read\n    return read()\n")
    assert "    from features.example.service import get as read" in result
    assert not review


def test_importing_submodule_from_package_preserves_local_binding():
    result, _ = apply("from core.services import old_service, other\n")
    assert result == "from features.example import service as old_service\nfrom core.services import other\n"


def test_relative_import_in_moved_module_resolves_against_old_package():
    result, _ = apply("from .helpers import helper\n", old="core.services.old_service")
    assert result == "from core.services.helpers import helper\n"


def test_relative_import_with_parent_keeps_original_dependency():
    result, _ = apply("from ..infra import logging\n", old="core.services.old_service")
    assert result == "from core.infra import logging\n"


def test_patch_and_monkeypatch_targets_are_updated():
    source = "from unittest import mock\ndef test_x(monkeypatch):\n    with mock.patch('core.services.old_service.get'):\n        monkeypatch.setattr('core.services.old_service.value', 1)\n"
    result, review = apply(source)
    assert "'features.example.service.get'" in result
    assert "'features.example.service.value'" in result
    assert not review


def test_unrecognized_string_is_not_changed_and_requires_review():
    source = "target = 'core.services.old_service.get'\n"
    result, review = apply(source)
    assert result == source
    assert review == [{"line": 1, "old": "core.services.old_service.get", "new": "features.example.service.get"}]


def test_unicode_and_multiple_imports_keep_valid_offsets():
    source = "标签 = '中文'; import core.services.old_service as svc\n"
    result, _ = apply(source)
    assert result == "标签 = '中文'; import features.example.service as svc\n"


def test_unaliased_dotted_import_updates_use_sites():
    result, _ = apply("import core.services.old_service\nx = core.services.old_service.get()\n")
    assert result == "import features.example.service\nx = features.example.service.get()\n"


def test_orm_symbol_moves_do_not_move_unrelated_models():
    result, _ = apply(
        "from core.db.models import Site, UserShadow\n",
        symbols={"Site": "features.sites.models"},
    )
    assert result == "from features.sites.models import Site\nfrom core.db.models import UserShadow\n"


def test_unrelated_import_preserves_multiline_comments():
    source = "from core.db.models import (\n    UserShadow,  # identity remains in core\n)\n"
    result, _ = apply(source)
    assert result == source


def test_embedded_module_reference_requires_review():
    source = "message = 'Cannot import core.services.old_service.get'\n"
    result, review = apply(source)
    assert result == source
    assert review[0]["new"] == "Cannot import features.example.service.get"


def test_split_import_after_semicolon_is_valid():
    result, _ = apply("x = 1; from core.services import old_service, other\n")
    assert result == "x = 1; from features.example import service as old_service; from core.services import other\n"


def test_parameter_shadowing_rejects_unaliased_rewrite():
    import pytest
    with pytest.raises(ValueError, match="ambiguous binding"):
        apply("import core.services.old_service\ndef read(core):\n    return core.services.old_service.get()\n")


def test_destination_root_shadowing_is_also_rejected():
    import pytest
    with pytest.raises(ValueError, match="ambiguous binding"):
        apply("import core.services.old_service\ndef read(features):\n    return core.services.old_service.get()\n")


def test_business_patch_function_is_not_a_mock():
    source = "def patch(value):\n    return value\nx = patch('core.services.old_service')\n"
    result, review = apply(source)
    assert result == source
    assert len(review) == 1


def test_imported_mock_alias_and_keyword_target_are_recognized():
    source = "from unittest.mock import patch as replace\nwith replace(target='core.services.old_service.get'):\n    pass\n"
    result, review = apply(source)
    assert "target='features.example.service.get'" in result
    assert not review


def test_nested_import_cannot_reclassify_outer_business_patch():
    source = "from application import patch\ndef test_real():\n    from unittest.mock import patch\n    return patch('core.services.old_service')\ndef unrelated():\n    return patch('core.services.old_service')\n"
    result, review = apply(source)
    assert result == source
    assert len(review) == 2


def test_wildcard_import_makes_mock_binding_ambiguous():
    source = "from unittest.mock import patch\nfrom business import *\npatch('core.services.old_service')\n"
    result, review = apply(source)
    assert result == source
    assert len(review) == 1


def test_relative_import_cannot_reclassify_business_patch():
    source = "from unittest.mock import patch\ndef f():\n    from .application import patch\n    return patch('core.services.old_service')\n"
    result, review = apply(source, old="core.example.caller")
    assert result == source
    assert len(review) == 1


def test_rebound_monkeypatch_fixture_requires_manual_review():
    for binding in ("from application import monkeypatch", "def monkeypatch(): pass", "class monkeypatch: pass"):
        source = "def test_x(monkeypatch):\n    " + binding + "\n    monkeypatch.setattr('core.services.old_service', 1)\n"
        result, review = apply(source)
        assert result == source
        assert len(review) == 1


def test_root_escape_after_namespace_move_requires_manual_review():
    import pytest
    with pytest.raises(ValueError, match="unresolved root use"):
        apply("import core.services.old_service\nmodule = core\n")
