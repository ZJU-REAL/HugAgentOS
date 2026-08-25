import assert from "node:assert/strict";
import { access, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import test from "node:test";

import { backupData, restoreData } from "../src/local-server.mjs";

test("local data backup and restore preserve Milvus directories", async () => {
  const root = await mkdtemp(join(tmpdir(), "hugagent-uos-backup-"));
  const dataRoot = join(root, "data");
  const backupsRoot = join(root, "backups");
  const milvusCollection = join(dataRoot, "milvus.db", "collections", "memories");

  try {
    await mkdir(milvusCollection, { recursive: true });
    await writeFile(join(dataRoot, "data.db"), "sqlite-before");
    await writeFile(join(milvusCollection, "vectors.bin"), "vectors-before");

    const backup = await backupData(dataRoot, backupsRoot);
    assert.ok(backup);
    assert.match(basename(backup), /^backup-/);
    assert.equal(await readFile(join(backup, "data.db"), "utf8"), "sqlite-before");
    assert.equal(
      await readFile(join(backup, "milvus.db", "collections", "memories", "vectors.bin"), "utf8"),
      "vectors-before",
    );

    await writeFile(join(dataRoot, "data.db"), "sqlite-after");
    await writeFile(join(dataRoot, "data.db-wal"), "new-wal");
    await writeFile(join(milvusCollection, "vectors.bin"), "vectors-after");
    await writeFile(join(milvusCollection, "stale.bin"), "stale");

    await restoreData(dataRoot, backup);

    assert.equal(await readFile(join(dataRoot, "data.db"), "utf8"), "sqlite-before");
    assert.equal(await readFile(join(milvusCollection, "vectors.bin"), "utf8"), "vectors-before");
    await assert.rejects(access(join(dataRoot, "data.db-wal")));
    await assert.rejects(access(join(milvusCollection, "stale.bin")));
    assert.deepEqual((await readdir(backupsRoot)).filter((name) => name.startsWith(".stage-")), []);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
