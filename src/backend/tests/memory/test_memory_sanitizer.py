"""Unit tests for core.memory.sanitizer.

These tests exercise the default hardcoded rules only; DB-override coverage
lives in an integration test that requires Postgres.
"""

from __future__ import annotations

import pytest

from core.memory.sanitizer import (
    CLASSIFIED_TERMS,
    REDACT_PATTERNS,
    SanitizeResult,
    invalidate_rules_cache,
    sanitize,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    """Clear the cache before each test to ensure the default rules take effect."""
    invalidate_rules_cache()
    yield
    invalidate_rules_cache()


# ─── Classified terms: reject ────────────────────────────────────────────────


@pytest.mark.parametrize("term", ["机密", "秘密", "绝密", "内部资料", "内部文件"])
def test_classified_cn_rejects(term: str) -> None:
    result = sanitize(f"这份材料标注了{term}，请勿外传")
    assert result.reject is True
    assert result.text is None
    assert result.hits == [f"classified:{term}"]


@pytest.mark.parametrize("term", ["Confidential", "Restricted", "NDA", "内部限阅"])
def test_classified_en_rejects(term: str) -> None:
    result = sanitize(f"Marked as {term} — do not share externally.")
    assert result.reject is True
    assert result.text is None


def test_clean_text_passes_through() -> None:
    result = sanitize("用户查询过 Q3 营收 32.1 亿元")
    assert result.reject is False
    assert result.text == "用户查询过 Q3 营收 32.1 亿元"
    assert result.hits == []
    assert result.clean is True


def test_empty_text() -> None:
    result = sanitize("")
    assert result.reject is False
    assert result.text == ""
    assert result.clean is True


def test_none_text() -> None:
    result = sanitize(None)  # type: ignore[arg-type]
    assert result.reject is False
    assert result.text == ""


# ─── Redact patterns ───────────────────────────────────────────────────────


def test_id_card_redacted() -> None:
    result = sanitize("我的身份证号是 310101199001011234")
    assert "[REDACTED:id_card]" in result.text
    assert "310101199001011234" not in result.text
    assert "id_card" in result.hits
    assert result.reject is False


def test_phone_cn_redacted() -> None:
    result = sanitize("联系电话 13812345678")
    assert "[REDACTED:phone_cn]" in result.text
    assert "13812345678" not in result.text
    assert "phone_cn" in result.hits


def test_email_redacted() -> None:
    result = sanitize("邮箱：user@example.com")
    assert "[REDACTED:email]" in result.text
    assert "user@example.com" not in result.text


def test_api_key_redacted() -> None:
    result = sanitize("API key: sk-abcdefghijklmnopqrstuvwxyz123456")
    assert "[REDACTED:api_key]" in result.text
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in result.text


def test_bearer_token_redacted() -> None:
    result = sanitize("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abcdefghij")
    assert "[REDACTED:api_key]" in result.text or "[REDACTED:jwt]" in result.text


def test_jwt_redacted() -> None:
    result = sanitize("Token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
    # A JWT may also be caught by api_key's Bearer match; as long as at least one hits and the original is replaced it's fine
    assert "[REDACTED:" in result.text
    assert "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9" not in result.text


def test_doc_no_redacted() -> None:
    result = sanitize("根据某政办〔2025〕12号文件")
    assert "[REDACTED:doc_no]" in result.text
    assert "某政办〔2025〕12号" not in result.text


def test_gov_serial_redacted() -> None:
    result = sanitize("财预字〔2025〕8")
    assert "[REDACTED:gov_serial]" in result.text


def test_customer_id_redacted() -> None:
    result = sanitize("客户编号 CUST-12345")
    assert "[REDACTED:customer_id]" in result.text


def test_internal_url_redacted() -> None:
    result = sanitize("查询后台 https://admin.internal/dashboard")
    assert "[REDACTED:internal_url]" in result.text
    assert "admin.internal" not in result.text


def test_bank_card_redacted() -> None:
    result = sanitize("卡号 6222020200123456789")
    assert "[REDACTED:bank_card]" in result.text


# ─── Combinations / boundaries ────────────────────────────────────────────────────────────


def test_multiple_patterns_all_hit() -> None:
    text = "张三 手机 13812345678 邮箱 z@z.cn 身份证 310101199001011234"
    result = sanitize(text)
    assert result.reject is False
    assert "13812345678" not in result.text
    assert "z@z.cn" not in result.text
    assert "310101199001011234" not in result.text
    assert set(result.hits) >= {"phone_cn", "email", "id_card"}


def test_classified_beats_redact() -> None:
    """When it contains both a classified term and PII, it should be rejected outright and not proceed to replacement."""
    text = "机密文件：电话 13812345678"
    result = sanitize(text)
    assert result.reject is True
    assert result.text is None


def test_non_id_number_not_falsely_redacted() -> None:
    """A 14-digit pure-numeric string should not be misclassified as id_card."""
    result = sanitize("订单号 12345678901234")
    # 14 digits is short of 15, so it should not match id_card
    assert "id_card" not in result.hits


def test_phone_inside_longer_digits_not_matched() -> None:
    """An 11-digit substring inside an 18-digit number should not be matched by phone_cn (boundaries apply)."""
    result = sanitize("订单 138123456780000")
    # id_card may hit (15-18 digits); phone_cn should not (protected by \d boundaries)
    assert "phone_cn" not in result.hits


def test_default_patterns_present() -> None:
    """Regression test: the core rule key names must not be accidentally changed."""
    required = {
        "id_card", "phone_cn", "email", "bank_card",
        "api_key", "jwt",
        "doc_no", "gov_serial",
        "customer_id", "internal_url",
    }
    assert required.issubset(REDACT_PATTERNS.keys())


def test_classified_terms_coverage() -> None:
    assert "机密" in CLASSIFIED_TERMS
    assert "NDA" in CLASSIFIED_TERMS
    assert "Confidential" in CLASSIFIED_TERMS


def test_result_clean_property() -> None:
    clean = SanitizeResult(text="hi", hits=[], reject=False)
    assert clean.clean is True

    dirty = SanitizeResult(text="hi [REDACTED:x]", hits=["x"], reject=False)
    assert dirty.clean is False

    rejected = SanitizeResult(text=None, hits=["classified:机密"], reject=True)
    assert rejected.clean is False
