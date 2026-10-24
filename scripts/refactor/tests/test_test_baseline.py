import pytest
from scripts.refactor.check_test_baseline import result


def test_completed_failure_summary_keeps_parameter_spaces():
    parsed = result("FAILED tests/test_x.py::test_case[some value] - AssertionError\n==== 1 failed, 3 passed in 2.02s ====\n")
    assert parsed["failures"] == ["tests/test_x.py::test_case[some value]"]


def test_incomplete_or_truncated_run_is_rejected():
    for log in ("tests/test_x.py F", "==== 1 failed in 2.02s ====", "INTERNALERROR> failure\n==== 1 passed in 2.02s ===="):
        with pytest.raises(ValueError):
            result(log)


def test_passed_run_has_empty_failure_set():
    assert result("==== 3 passed, 1 skipped in 2.02s ====\n")["failures"] == []


def test_application_error_logs_are_not_test_outcomes():
    log = "ERROR unrelated application log\n==== short test summary info ====\nFAILED tests/test_x.py::test_case\n==== 1 failed in 2.02s ====\n"
    assert result(log)["failures"] == ["tests/test_x.py::test_case"]
