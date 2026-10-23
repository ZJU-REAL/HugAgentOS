"""History parsing preserves the concrete adapter method binding."""
from integrations.channels.adapters.dingtalk import DingTalkAdapter


def test_history_parser_uses_adapter_file_and_timestamp_methods(monkeypatch):
    calls = []
    def files(text):
        calls.append(text)
        return "parsed", [{"key": "file"}]
    monkeypatch.setattr(DingTalkAdapter, "_extract_dws_files", staticmethod(files))
    monkeypatch.setattr(DingTalkAdapter, "_dws_ts_ms", staticmethod(lambda value: 123))
    items = DingTalkAdapter._parse_dws_history({"messages": [{"content": "source", "createTime": 1}]})
    assert calls == ["source"]
    assert [(item.text, item.ts_ms, item.attachments) for item in items] == [
        ("parsed", 123, [{"key": "file"}])
    ]
