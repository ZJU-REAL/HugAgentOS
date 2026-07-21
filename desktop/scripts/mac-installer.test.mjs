import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const installer = join(
  desktopDir,
  "resources",
  "server-bootstrap",
  "install-local-server.sh",
);

function writeExecutable(path, contents) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, contents, "utf8");
  chmodSync(path, 0o755);
}

test("macOS bootstrap completes a clean CE install with an isolated runtime", () => {
  const fixture = mkdtempSync(join(tmpdir(), "hugagent-macos-installer-"));
  const bundle = join(fixture, "bundle");
  const installRoot = join(fixture, "installed");

  try {
    mkdirSync(join(bundle, "src", "frontend", "dist"), { recursive: true });
    mkdirSync(join(bundle, "docker"), { recursive: true });
    writeFileSync(join(bundle, "pyproject.toml"), "[project]\nname='test'\n");
    writeFileSync(join(bundle, "requirements.txt"), "");
    writeFileSync(join(bundle, "docker", "requirements-script-runner.txt"), "");
    writeFileSync(join(bundle, "src", "frontend", "dist", "index.html"), "ok");
    writeFileSync(join(bundle, "desktop-bundle.json"), '{"desktop_version":"test"}\n');

    writeExecutable(
      join(installRoot, "tools", "uv"),
      `#!/bin/bash
set -e
if [[ "$1" == "venv" ]]; then
  destination="\${!#}"
  mkdir -p "$destination/bin"
  printf '#!/bin/bash\\nexit 0\\n' > "$destination/bin/python"
  chmod +x "$destination/bin/python"
elif [[ "$1" == "pip" && " $* " == *" --editable "* ]]; then
  previous=""
  python=""
  for argument in "$@"; do
    if [[ "$previous" == "--python" ]]; then python="$argument"; fi
    previous="$argument"
  done
  printf '#!/bin/bash\\nexit 0\\n' > "$(dirname "$python")/hugagent"
  chmod +x "$(dirname "$python")/hugagent"
fi
`,
    );

    const result = spawnSync(
      "/bin/bash",
      [
        installer,
        "--bundle-dir",
        bundle,
        "--install-root",
        installRoot,
      ],
      {
        encoding: "utf8",
        env: { ...process.env, HUGAGENT_SKIP_OPTIONAL_NODE: "1" },
      },
    );

    assert.equal(result.status, 0, result.stderr || result.stdout);
    assert.match(result.stdout, /HUGAGENT_PROGRESS\|90\|/);
    assert.equal(
      readFileSync(join(installRoot, "installed-bundle.json"), "utf8"),
      '{"desktop_version":"test"}\n',
    );
    assert.equal(
      readFileSync(join(installRoot, "source", "src", "frontend", "dist", "index.html"), "utf8"),
      "ok",
    );
  } finally {
    rmSync(fixture, { recursive: true, force: true });
  }
});
