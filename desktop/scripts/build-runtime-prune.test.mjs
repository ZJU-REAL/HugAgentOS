import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { pruneBytecodeCaches, pruneSitePackageTests } from "./build-runtime.mjs";

function fixture(files) {
  const root = mkdtempSync(join(tmpdir(), "runtime-prune-"));
  for (const file of files) {
    mkdirSync(join(root, file, ".."), { recursive: true });
    writeFileSync(join(root, file), "");
  }
  return root;
}

test("removes test-suite directories from site-packages but keeps runtime modules", (t) => {
  const site = "lib/python3.11/site-packages";
  const root = fixture([
    `${site}/pandas/tests/test_frame.py`,
    `${site}/scipy/stats/tests/test_stats.py`,
    `${site}/posthog/test/test_client.py`,
    `${site}/werkzeug/test.py`,
    `${site}/jinja2/tests.py`,
    `${site}/numpy/testing/__init__.py`,
    // Windows layout, and the standard library outside site-packages.
    "Lib/site-packages/pandas/tests/test_frame.py",
    "lib/python3.11/unittest/test/test_case.py",
  ]);
  t.after(() => rmSync(root, { recursive: true, force: true }));

  assert.equal(pruneSitePackageTests(root), 4);
  assert.equal(existsSync(join(root, site, "pandas/tests")), false);
  assert.equal(existsSync(join(root, site, "scipy/stats/tests")), false);
  assert.equal(existsSync(join(root, site, "posthog/test")), false);
  assert.equal(existsSync(join(root, "Lib/site-packages/pandas/tests")), false);
  assert.ok(existsSync(join(root, site, "werkzeug/test.py")));
  assert.ok(existsSync(join(root, site, "jinja2/tests.py")));
  assert.ok(existsSync(join(root, site, "numpy/testing/__init__.py")));
  assert.ok(existsSync(join(root, "lib/python3.11/unittest/test/test_case.py")));
});

test("removes every __pycache__ without following symlinks", (t) => {
  const root = fixture([
    "lib/python3.11/__pycache__/os.cpython-311.pyc",
    "lib/python3.11/site-packages/numpy/__pycache__/__init__.cpython-311.pyc",
    "lib/python3.11/site-packages/numpy/__init__.py",
  ]);
  const outside = fixture(["__pycache__/keep.pyc"]);
  symlinkSync(outside, join(root, "linked"));
  t.after(() => {
    rmSync(root, { recursive: true, force: true });
    rmSync(outside, { recursive: true, force: true });
  });

  assert.equal(pruneBytecodeCaches(root), 2);
  assert.equal(existsSync(join(root, "lib/python3.11/__pycache__")), false);
  assert.equal(existsSync(join(root, "lib/python3.11/site-packages/numpy/__pycache__")), false);
  assert.ok(existsSync(join(root, "lib/python3.11/site-packages/numpy/__init__.py")));
  assert.ok(existsSync(join(outside, "__pycache__/keep.pyc")));
});
