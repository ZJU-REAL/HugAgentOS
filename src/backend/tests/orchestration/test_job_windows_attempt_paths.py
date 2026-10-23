"""Windows attempts keep the full unique id while avoiding excess nesting."""

import core.jobs.process as jobs_process
import core.jobs.process as process


def test_native_windows_attempt_path_keeps_sdk_below_max_path(monkeypatch):
    workspace = "C:/" + "a" * 184
    monkeypatch.setattr(jobs_process, "_job_directory", lambda session, job: workspace + "/.job/" + job)
    job = "job_" + "1" * 16
    attempt = "2" * 32
    old = process.attempt_directory("chat", job, attempt, windows_local=False)
    new = process.attempt_directory("chat", job, attempt, windows_local=True)
    assert len(old + "/hugagent_job.py") >= 260
    assert len(new + "/hugagent_job.py") < 260
    assert new.endswith("/.job/" + attempt)
    assert old.endswith("/.job/" + job + "/" + attempt)
    assert process.attempt_directory("chat", job, "3" * 32, windows_local=True) != new
