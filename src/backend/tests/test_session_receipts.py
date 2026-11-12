"""The shared receipt protocol (hugagent_cli.contract): one place decides what a failure means."""

import pytest

from hugagent_cli import contract as c


def test_reads_never_have_an_unknown_outcome():
    for status in (None, 404, 500, 503):
        receipt = c.failure("chat.list", "x", sent=True, http_status=status)
        assert receipt["not_executed"] is True and "outcome_unknown" not in receipt


def test_nothing_sent_means_not_executed():
    receipt = c.failure("chat.send", "local_failure", sent=False, exception="AttributeError")
    assert receipt == {"error": "local_failure", "not_executed": True, "exception": "AttributeError", "retryable": True}


def test_a_write_refused_by_the_route_did_not_run():
    receipt = c.failure("chat.send", "http_409", sent=True, http_status=409, detail="chat_busy")
    assert receipt["not_executed"] is True and receipt["retryable"] is False
    assert receipt["http_status"] == 409 and receipt["detail"] == "chat_busy"
    assert c.failure("chat.send", "http_429", sent=True, http_status=429)["retryable"] is True


@pytest.mark.parametrize("status", [None, 500, 502])
def test_a_sent_write_without_a_definite_answer_is_uncertain(status):
    receipt = c.failure("chat.delete", "timed_out", sent=True, http_status=status)
    assert receipt["outcome_unknown"] is True and "not_executed" not in receipt


def test_detail_is_bounded():
    receipt = c.not_executed("http_500", detail="x" * 2000)
    assert len(receipt["detail"]) == c.DETAIL_LIMIT + 1


def test_receipt_status_reads_old_and_new_receipts():
    assert c.receipt_status({"items": []}) == c.STATUS_COMPLETED
    assert c.receipt_status([1, 2]) == c.STATUS_COMPLETED
    assert c.receipt_status(None) == c.STATUS_COMPLETED
    assert c.receipt_status({"deleted": True}) == c.STATUS_COMPLETED
    # What 0.5.163 desktops send.
    assert c.receipt_status({"outcome_unknown": True, "error": "local_failure; check the result before retry"}) == c.STATUS_OUTCOME_UNKNOWN
    assert c.receipt_status({"error": "permission_revoked", "not_executed": True}) == c.STATUS_NOT_EXECUTED
    assert c.receipt_status({"error": "something"}) == c.STATUS_FAILED


def test_steer_id_follows_the_route_limit():
    c.validate("run.steer", {"run_id": "r1", "message": "m", "steer_id": "s" * 64})
    with pytest.raises(ValueError):
        c.validate("run.steer", {"run_id": "r1", "message": "m", "steer_id": "s" * 65})


def test_contract_version_is_stable_and_tracks_the_contract(monkeypatch):
    first = c.contract_version()
    assert first == c.contract_version() and len(first) == 16
    monkeypatch.setitem(c.FIELDS, "chat.list", c.FIELDS["chat.list"] | {"extra"})
    assert c.contract_version() != first
