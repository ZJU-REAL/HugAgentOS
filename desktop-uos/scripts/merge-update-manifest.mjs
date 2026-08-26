import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { copyFile, mkdir, readFile, readdir, rename, stat, writeFile } from "node:fs/promises";
import { basename, dirname, join, resolve } from "node:path";

const desktopDir = resolve(import.meta.dirname, "..");
const args = new Map();
for (let index = 2; index < process.argv.length; index += 1) {
  const key = process.argv[index];
  if (!key.startsWith("--") || !process.argv[index + 1]) throw new Error(`参数无效：${key}`);
  args.set(key.slice(2), process.argv[++index]);
}
const packageJson = JSON.parse(await readFile(join(desktopDir, "package.json"), "utf8"));
const dist = join(desktopDir, "dist");
const defaultDeb = (await readdir(dist).catch(() => [])).find((name) => name.endsWith(".deb"));
const deb = resolve(args.get("deb") || join(dist, defaultDeb || "missing.deb"));
const version = args.get("version") || packageJson.version;
const outputDir = resolve(args.get("output") || join(dist, "release"));
const existingPath = args.get("existing") ? resolve(args.get("existing")) : join(outputDir, "latest.json");
if (!basename(deb).endsWith(".deb")) throw new Error("UOS 更新产物必须是 .deb");
if (!/^\d+\.\d+\.\d+/.test(version)) throw new Error(`版本号无效：${version}`);

async function sha256(path) {
  const hash = createHash("sha256");
  await new Promise((resolveHash, reject) => {
    const stream = createReadStream(path);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("error", reject);
    stream.on("end", resolveHash);
  });
  return hash.digest("hex");
}

let existing = null;
try { existing = JSON.parse(await readFile(existingPath, "utf8")); } catch {}
const platforms = existing?.version === version && existing.platforms && typeof existing.platforms === "object"
  ? { ...existing.platforms }
  : {};
const destination = join(outputDir, basename(deb));
const info = await stat(deb);
platforms["linux-aarch64"] = {
  url: basename(deb),
  sha256: await sha256(deb),
  size: info.size,
  format: "deb",
};
const manifest = {
  version,
  notes: args.get("notes") || "",
  pub_date: new Date().toISOString(),
  platforms,
};
await mkdir(outputDir, { recursive: true });
if (deb !== destination) await copyFile(deb, destination);
const next = join(outputDir, `.latest-${process.pid}.json`);
await writeFile(next, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
await rename(next, join(outputDir, "latest.json"));
console.log(`[desktop-uos] release directory ready: ${outputDir}`);
console.log(`[desktop-uos] platform linux-aarch64 sha256=${platforms["linux-aarch64"].sha256}`);
