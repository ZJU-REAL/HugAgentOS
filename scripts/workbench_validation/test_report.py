"""Evidence boundary tests. These do not validate application behavior."""
import tempfile
import unittest
from pathlib import Path

from scripts.workbench_validation.report import merge_results
from scripts.workbench_validation.matrix import required_cases
from scripts.workbench_validation.batch import run_command, SCRATCH


class EvidenceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "evidence.json").write_text('{"observed":"public behavior"}')
        self.cases = [dict(id="A07", level="real-browser", status="not-run")]
        self.result = dict(id="A07", status="pass", level="real-browser",
                           sourceFingerprint="frozen", actual="cross-user launch denied",
                           steps=["open as another isolated user"],
                           evidence=["evidence.json"])

    def test_fixture_cannot_satisfy_real_browser_acceptance(self):
        self.result["level"] = "fixture-browser"
        with self.assertRaisesRegex(ValueError, "Evidence level"):
            merge_results(self.root, self.cases, [self.result], "frozen")

    def test_stale_results_cannot_satisfy_new_source(self):
        with self.assertRaisesRegex(ValueError, "Stale"):
            merge_results(self.root, self.cases, [self.result], "changed")

    def test_pass_requires_reproducible_steps_and_existing_evidence(self):
        self.result["steps"] = []
        with self.assertRaisesRegex(ValueError, "Pass requires"):
            merge_results(self.root, self.cases, [self.result], "frozen")
        self.result["steps"] = ["reproduce"]
        self.result["evidence"] = ["missing.json"]
        with self.assertRaisesRegex(ValueError, "Missing"):
            merge_results(self.root, self.cases, [self.result], "frozen")

    def test_duplicate_or_unknown_results_do_not_override(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            merge_results(self.root, self.cases, [self.result, self.result], "frozen")
        self.result["id"] = "unknown"
        with self.assertRaisesRegex(ValueError, "Unknown"):
            merge_results(self.root, self.cases, [self.result], "frozen")

    def test_evidence_path_cannot_escape_checkout(self):
        self.result["evidence"] = ["../outside"]
        with self.assertRaisesRegex(ValueError, "out-of-scope"):
            merge_results(self.root, self.cases, [self.result], "frozen")

    def test_perf_cannot_pass_without_metrics_or_with_over_budget_p95(self):
        case = dict(id="PERF-100", level="real-browser", status="not-run")
        self.result["id"] = "PERF-100"
        with self.assertRaisesRegex(ValueError, "performance"):
            merge_results(self.root, [case], [self.result], "frozen")
        self.result["performance"] = dict(sampleCount=100, p95Ms=101, applicationCount=100,
                                         unopenedIframeCount=0, unopenedProcessCount=0,
                                         cpu="test CPU", memoryBytes=1, browserVersion="test",
                                         buildMode="production")
        with self.assertRaisesRegex(ValueError, "performance"):
            merge_results(self.root, [case], [self.result], "frozen")

    def test_perf_rejects_zero_counts_without_actual_provider_observation(self):
        case = dict(id="PERF-100", level="real-browser", status="not-run")
        self.result["id"] = "PERF-100"
        self.result["performance"] = dict(sampleCount=100, p95Ms=16, applicationCount=100,
                                         unopenedIframeCount=0, unopenedProcessCount=0,
                                         cpu="test CPU", memoryBytes=1, browserVersion="test",
                                         buildMode="production")
        with self.assertRaisesRegex(ValueError, "performance"):
            merge_results(self.root, [case], [self.result], "frozen")

    def test_ui_case_cannot_pass_using_wrong_dimensions(self):
        case = dict(id="UI-modern-100-480-dark-en", level="real-browser", status="not-run",
                    layout="modern", applications=100, height=480, theme="dark", language="en")
        self.result["id"] = case["id"]
        self.result["dimensions"] = dict(layout="modern", applications=5, height=480,
                                         theme="dark", language="en")
        with self.assertRaisesRegex(ValueError, "dimension"):
            merge_results(self.root, [case], [self.result], "frozen")

    def test_db_driver_query_cannot_override_disposable_target(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        command = dict(id="unsafe-db", reviewed=True, cwd=".", level="real-http",
                       argv=["python3", "-c", "print('must not run')"],
                       env={"DATABASE_URL": "postgresql://localhost/workbench_23_test?host=remote&dbname=production"})
        with tempfile.TemporaryDirectory(dir=SCRATCH) as output:
            with self.assertRaisesRegex(ValueError, "isolated"):
                run_command(command, Path(output), 0)

    def test_command_arguments_are_not_serialized_by_default(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        command = dict(id="safe-cli", reviewed=True, cwd=".", level="static",
                       argv=["python3", "-c", "pass", "--password", "literal-private-value"])
        with tempfile.TemporaryDirectory(dir=SCRATCH) as output:
            result = run_command(command, Path(output), 0)
            self.assertNotIn("literal-private-value", str(result))

    def test_verified_result_preserves_evidence_hash(self):
        result = merge_results(self.root, self.cases, [self.result], "frozen")
        self.assertEqual(result[0]["status"], "pass")
        self.assertEqual(len(result[0]["evidence"][0]["sha256"]), 64)

    def test_matrix_preserves_all_web_acceptance_and_dimensions(self):
        root = Path(__file__).resolve().parents[2]
        cases = required_cases(root)
        ids = {case["id"] for case in cases}
        self.assertTrue({f"A{i:02}" for i in range(1, 17) if i != 13}.issubset(ids))
        self.assertEqual(len([case for case in cases if case["id"].startswith("UI-")]), 72)
        self.assertTrue(all(case["status"] == "not-run" for case in cases))
        self.assertGreater(len([case for case in cases if case["id"].startswith("R")]), 15)
        self.assertEqual(len({case["feature"] for case in cases if "feature" in case}), 15)


if __name__ == "__main__":
    unittest.main()
