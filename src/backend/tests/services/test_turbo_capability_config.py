"""极速模式可装配能力（技能 / 插件）的共享配置解析。

覆盖 ``turbo.skill_ids`` / ``turbo.plugin_ids`` 的解析——逗号分隔、去空白、
去重，配置层异常时按「没配」处理（不能因为读配置失败就把能力凭空塞进
极速模式）。EE 控制面选项接口的覆盖位于独立的 EE-only 测试文件中。
"""

import pytest
from core.services import system_config as sysconf


class _StubConfigService:
    def __init__(self, values: dict, *, raises: bool = False):
        self._values = values
        self._raises = raises

    def get(self, key, default=None):
        if self._raises:
            raise RuntimeError("config layer down")
        return self._values.get(key, default)


def _patch_config(monkeypatch, values, *, raises=False):
    stub = _StubConfigService(values, raises=raises)
    monkeypatch.setattr(sysconf.SystemConfigService, "get_instance", classmethod(lambda cls: stub))


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ()),
        (None, ()),
        ("officecli-docx", ("officecli-docx",)),
        (" a , b ,, a ", ("a", "b")),
    ],
)
def test_turbo_skill_ids_parsing(monkeypatch, raw, expected):
    _patch_config(monkeypatch, {"turbo.skill_ids": raw})
    assert sysconf.turbo_skill_ids() == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ()),
        ("plugin-a@global", ("plugin-a@global",)),
        (
            "plugin-a@global, plugin-b@global ,plugin-a@global",
            ("plugin-a@global", "plugin-b@global"),
        ),
    ],
)
def test_turbo_plugin_ids_parsing(monkeypatch, raw, expected):
    _patch_config(monkeypatch, {"turbo.plugin_ids": raw})
    assert sysconf.turbo_plugin_ids() == expected


def test_turbo_capability_ids_empty_when_config_layer_fails(monkeypatch):
    _patch_config(monkeypatch, {}, raises=True)
    assert sysconf.turbo_skill_ids() == ()
    assert sysconf.turbo_plugin_ids() == ()
