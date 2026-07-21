import assert from "node:assert/strict";
import {
  existsSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { spawnSync } from "node:child_process";

import {
  assertDerivedCeRepository,
  stageTrackedCeRepository,
} from "./ce-payload.mjs";

function git(root, args) {
  const result = spawnSync("git", args, {
    cwd: root,
    encoding: "utf8",
    shell: false,
  });
  if (result.status !== 0)
    throw new Error(result.stderr || `git ${args.join(" ")} failed`);
}

function createCeFixture() {
  const root = mkdtempSync(join(tmpdir(), "hugagent-ce-payload-"));
  mkdirSync(join(root, "desktop"), { recursive: true });
  mkdirSync(join(root, "src", "backend"), { recursive: true });
  writeFileSync(join(root, ".hugagent-edition"), "ce\n");
  writeFileSync(
    join(root, ".gitignore"),
    "desktop/generated/\nuntracked.txt\n",
  );
  writeFileSync(join(root, "src", "backend", "app.py"), 'print("tracked")\n');
  writeFileSync(join(root, "untracked.txt"), "do not bundle\n");
  git(root, ["init", "--quiet"]);
  git(root, ["config", "user.name", "Release Test"]);
  git(root, ["config", "user.email", "release-test@example.com"]);
  git(root, ["add", ".hugagent-edition", ".gitignore", "src/backend/app.py"]);
  git(root, ["commit", "--quiet", "-m", "fixture"]);
  return root;
}

test("stages only tracked files from an already-derived CE repository", () => {
  const root = createCeFixture();
  const output = join(root, "desktop", "generated", "server-ce");
  try {
    assert.equal(
      stageTrackedCeRepository(root, output, { requireClean: true }),
      3,
    );
    assert.equal(
      readFileSync(join(output, ".hugagent-edition"), "utf8"),
      "ce\n",
    );
    assert.equal(
      readFileSync(join(output, "src", "backend", "app.py"), "utf8"),
      'print("tracked")\n',
    );
    assert.equal(existsSync(join(output, "untracked.txt")), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("rejects release staging from a dirty CE repository", () => {
  const root = createCeFixture();
  const output = join(root, "desktop", "generated", "server-ce");
  try {
    writeFileSync(join(root, "src", "backend", "app.py"), 'print("dirty")\n');
    assert.throws(
      () => stageTrackedCeRepository(root, output, { requireClean: true }),
      /clean Git checkout/,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("rejects fallback staging without the derived CE marker", () => {
  const root = mkdtempSync(join(tmpdir(), "hugagent-not-ce-"));
  try {
    assert.throws(
      () => assertDerivedCeRepository(root),
      /does not identify this checkout/,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("rejects a payload output outside the repository", () => {
  const root = createCeFixture();
  const output = mkdtempSync(join(tmpdir(), "hugagent-ce-output-"));
  try {
    assert.throws(
      () => stageTrackedCeRepository(root, output),
      /inside the repository/,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
    rmSync(output, { recursive: true, force: true });
  }
});
