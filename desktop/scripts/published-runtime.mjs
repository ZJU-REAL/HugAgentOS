import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { copyFileSync, existsSync, readFileSync, renameSync, rmSync } from "node:fs";
import { join } from "node:path";

// A runtime is built once per dependency fingerprint. Later releases reuse the
// published bytes so the full installer, the slim update package and the file
// clients download all agree on archive_sha256.

export function publishedRuntimeNames(target, fingerprint) {
  if (!/^[a-z0-9_-]+$/.test(target) || !/^[a-f0-9]{64}$/.test(fingerprint)) {
    throw new Error(`Invalid runtime identity: ${target}/${fingerprint}`);
  }
  const stem = `runtime-${target}-${fingerprint}`;
  return { archive: `${stem}.tar.gz`, manifest: `${stem}.json` };
}

function hashFile(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

// Returns the HTTP status; the body is written to `output` only on 200.
function download(url, output) {
  const partial = `${output}.part`;
  rmSync(partial, { force: true });
  const result = spawnSync(
    "curl",
    ["-sS", "-L", "--retry", "3", "-o", partial, "-w", "%{http_code}", url],
    { encoding: "utf8", shell: false },
  );
  if (result.error) throw result.error;
  if (result.status !== 0) {
    rmSync(partial, { force: true });
    throw new Error(`Downloading ${url} failed: ${result.stderr.trim()}`);
  }
  const status = Number(result.stdout.trim());
  if (status === 200) renameSync(partial, output);
  else rmSync(partial, { force: true });
  return status;
}

function fetchInto(source, name, output) {
  if (/^https?:\/\//i.test(source)) {
    const url = `${source.replace(/\/+$/, "")}/api/v1/desktop/download/${name}`;
    const status = download(url, output);
    if (status === 404) return false;
    if (status !== 200) throw new Error(`Downloading ${url} returned HTTP ${status}`);
    return true;
  }
  const path = join(source, name);
  if (!existsSync(path)) return false;
  copyFileSync(path, output);
  return true;
}

/**
 * Fetch the runtime already published for this fingerprint into
 * `archivePath` / `manifestPath`. `source` is a release server base URL or a
 * local release directory (HUGAGENT_RUNTIME_SOURCE). Returns false when this
 * fingerprint has never been published, so the caller builds it.
 */
export function fetchPublishedRuntime({ source, target, fingerprint, archivePath, manifestPath }) {
  if (!source) return false;
  const names = publishedRuntimeNames(target, fingerprint);
  const stagedManifest = `${manifestPath}.published`;
  const stagedArchive = `${archivePath}.published`;
  try {
    if (!fetchInto(source, names.manifest, stagedManifest)) {
      console.log(`[desktop] No published runtime for ${target}/${fingerprint.slice(0, 12)}; building it`);
      return false;
    }
    const manifest = JSON.parse(readFileSync(stagedManifest, "utf8"));
    if (manifest.target !== target || manifest.dependency_fingerprint !== fingerprint) {
      throw new Error(`Published runtime manifest ${names.manifest} describes another runtime`);
    }
    console.log(`[desktop] Downloading published runtime ${names.archive}`);
    if (!fetchInto(source, names.archive, stagedArchive)) {
      throw new Error(`Published runtime manifest exists but ${names.archive} is missing`);
    }
    if (hashFile(stagedArchive) !== manifest.archive_sha256) {
      throw new Error(`Published runtime ${names.archive} does not match its manifest`);
    }
    renameSync(stagedArchive, archivePath);
    renameSync(stagedManifest, manifestPath);
    return true;
  } finally {
    rmSync(stagedManifest, { force: true });
    rmSync(stagedArchive, { force: true });
  }
}
