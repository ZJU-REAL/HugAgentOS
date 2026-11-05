import { existsSync, mkdirSync, readdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

// The slim update package is the full desktop build with an empty runtime
// placeholder. Clients download the runtime named in runtime-manifest.json
// only when its dependency fingerprint is new to them.

const UPDATE_BUNDLES = Object.freeze({ win32: ["nsis"], darwin: ["app"], linux: ["appimage"] });

export function updateBundleTargets(platform = process.platform) {
  const bundles = UPDATE_BUNDLES[platform];
  if (!bundles) throw new Error(`No slim update package for ${platform}`);
  return bundles;
}

/** Tauri `--config` merge patch applied on top of the platform overlay. */
export function updateBundleConfig(platform = process.platform) {
  return {
    build: {
      // generated/ was prepared by the full build that must run first.
      beforeBuildCommand: "node --version",
    },
    bundle: {
      targets: updateBundleTargets(platform),
      resources: {
        "../generated/runtime-core.tar.gz": null,
        "../generated/update/runtime-core.tar.gz": "runtime-core.tar.gz",
      },
    },
  };
}

/** Insert `-update` so publishing, downloads and installers tell the packages apart. */
export function slimArtifactName(name) {
  const rules = [
    [/^(.*)(-setup\.exe(?:\.sig)?)$/, "$1-update$2"],
    [/^(.*)(\.app\.tar\.gz(?:\.sig)?)$/, "$1-update$2"],
    [/^(.*)(\.AppImage(?:\.sig)?)$/, "$1-update$2"],
  ];
  for (const [pattern, replacement] of rules) {
    if (pattern.test(name)) return name.replace(pattern, replacement);
  }
  return null;
}

export function writeUpdateBundleInputs(desktopDir, platform = process.platform) {
  const generated = join(desktopDir, "generated");
  for (const name of ["runtime-core.tar.gz", "runtime-manifest.json", "server-ce.zip"]) {
    if (!existsSync(join(generated, name))) {
      throw new Error(`generated/${name} is missing; run the full build before the update package`);
    }
  }
  mkdirSync(join(generated, "update"), { recursive: true });
  writeFileSync(join(generated, "update", "runtime-core.tar.gz"), "");
  const configPath = join(generated, "update", "tauri.update.conf.json");
  writeFileSync(configPath, `${JSON.stringify(updateBundleConfig(platform), null, 2)}\n`);
  return configPath;
}

/**
 * Both builds write the same bundle directory. Park the full bundle, let the
 * update build write its own, then move that aside as `bundle-update` with
 * renamed artifacts and restore the full bundle.
 */
export function withParkedFullBundle(releaseDir, build) {
  const bundle = join(releaseDir, "bundle");
  const parked = join(releaseDir, "bundle-full");
  const update = join(releaseDir, "bundle-update");
  if (!existsSync(bundle)) throw new Error(`${bundle} is missing; run the full build first`);
  rmSync(parked, { recursive: true, force: true });
  rmSync(update, { recursive: true, force: true });
  renameSync(bundle, parked);
  try {
    build();
    renameSync(bundle, update);
  } finally {
    rmSync(bundle, { recursive: true, force: true });
    renameSync(parked, bundle);
  }
  const renamed = [];
  for (const kind of readdirSync(update)) {
    const directory = join(update, kind);
    for (const name of readdirSync(directory)) {
      const slim = slimArtifactName(name);
      if (!slim) continue;
      renameSync(join(directory, name), join(directory, slim));
      renamed.push(join(directory, slim));
    }
  }
  if (!renamed.length) throw new Error(`The update build produced no updater artifact in ${update}`);
  return update;
}
