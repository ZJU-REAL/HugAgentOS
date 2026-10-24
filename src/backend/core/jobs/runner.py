"""Runner delivered into an attempt directory; no backend imports in sandbox."""

RUNNER_SOURCE = r"""
import json
import os
import runpy
import sys
import traceback

with open("execution.json", encoding="utf-8") as fh:
    config = json.load(fh)
os.environ.update(config)
import hugagent_job as hj

def record(status, error=""):
    # Disk receipt precedes HTTP: even a rejected/lost callback has evidence.
    data = {"attempt_id": config["JOB_ATTEMPT_ID"], "status": status,
            "error": str(error)[-4000:]}
    with open("lifecycle.final.tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace("lifecycle.final.tmp", "lifecycle.final")
    try:
        hj._lifecycle(status, error)
    except Exception as exc:
        print("Lifecycle delivery failed: %s" % type(exc).__name__, flush=True)

try:
    hj._lifecycle("running")
    runpy.run_path("user_script.py", run_name="__main__")
except SystemExit as exc:
    code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    record("completed" if code == 0 else "failed",
           "" if code == 0 else "Script exited with code %s" % code)
    sys.exit(code)
except BaseException:
    error = traceback.format_exc()[-4000:]
    record("failed", error)
    print(error, flush=True)
    sys.exit(1)
else:
    record("completed")
"""
