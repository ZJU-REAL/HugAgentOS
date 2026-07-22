import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
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
  const uvLog = join(fixture, "uv.log");

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
if [[ "$1" == "--system-certs" ]]; then shift; fi
printf '%s\n' "$*" >> "\${HUGAGENT_UV_LOG:?}"
if [[ " $* " == *" --prefer-binary "* ]]; then
  echo "unexpected pip-only argument: --prefer-binary" >&2
  exit 2
fi
if [[ "\${HUGAGENT_FAIL_REQUIREMENTS:-0}" == "1" && "$1" == "pip" && " $* " == *" --requirements "* ]]; then
  exit 9
fi
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
        env: {
          ...process.env,
          HUGAGENT_SKIP_OPTIONAL_NODE: "1",
          HUGAGENT_UV_LOG: uvLog,
        },
      },
    );

    assert.equal(result.status, 0, result.stderr || result.stdout);
    assert.match(result.stdout, /HUGAGENT_PROGRESS\|90\|/);
    assert.equal(
      readFileSync(
        join(installRoot, "current", "desktop-bundle.json"),
        "utf8",
      ),
      '{"desktop_version":"test"}\n',
    );
    assert.equal(
      readFileSync(
        join(
          installRoot,
          "current",
          "source",
          "src",
          "frontend",
          "dist",
          "index.html",
        ),
        "utf8",
      ),
      "ok",
    );
    const uvCalls = readFileSync(uvLog, "utf8");
    assert.match(uvCalls, /--overrides .*requirements-macos-overrides\.txt/);
    assert.match(uvCalls, /--only-binary pikepdf/);
    assert.doesNotMatch(uvCalls, /--prefer-binary/);

    writeFileSync(
      join(bundle, "src", "frontend", "dist", "index.html"),
      "updated",
    );
    writeFileSync(
      join(bundle, "desktop-bundle.json"),
      '{"desktop_version":"updated"}\n',
    );
    const update = spawnSync(
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
        env: {
          ...process.env,
          HUGAGENT_SKIP_OPTIONAL_NODE: "1",
          HUGAGENT_UV_LOG: uvLog,
        },
      },
    );
    assert.equal(update.status, 0, update.stderr || update.stdout);
    assert.equal(
      readFileSync(
        join(
          installRoot,
          "current",
          "source",
          "src",
          "frontend",
          "dist",
          "index.html",
        ),
        "utf8",
      ),
      "updated",
    );
    assert.equal(
      readFileSync(
        join(
          installRoot,
          "current.previous",
          "source",
          "src",
          "frontend",
          "dist",
          "index.html",
        ),
        "utf8",
      ),
      "ok",
    );

    writeFileSync(
      join(bundle, "desktop-bundle.json"),
      '{"desktop_version":"third"}\n',
    );
    const third = spawnSync(
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
        env: {
          ...process.env,
          HUGAGENT_SKIP_OPTIONAL_NODE: "1",
          HUGAGENT_UV_LOG: uvLog,
        },
      },
    );
    assert.equal(third.status, 0, third.stderr || third.stdout);
    assert.equal(readdirSync(join(installRoot, "releases")).length, 2);
  } finally {
    rmSync(fixture, { recursive: true, force: true });
  }
});

test("macOS bootstrap leaves the previous release untouched after dependency failure", () => {
  const fixture = mkdtempSync(join(tmpdir(), "hugagent-macos-rollback-"));
  const bundle = join(fixture, "bundle");
  const installRoot = join(fixture, "installed");
  const uvLog = join(fixture, "uv.log");

  try {
    mkdirSync(join(bundle, "src", "frontend", "dist"), { recursive: true });
    mkdirSync(join(bundle, "docker"), { recursive: true });
    writeFileSync(join(bundle, "pyproject.toml"), "[project]\nname='test'\n");
    writeFileSync(join(bundle, "requirements.txt"), "broken>=1\n");
    writeFileSync(join(bundle, "docker", "requirements-script-runner.txt"), "");
    writeFileSync(join(bundle, "src", "frontend", "dist", "index.html"), "new");
    writeFileSync(join(bundle, "desktop-bundle.json"), '{"desktop_version":"new"}\n');

    mkdirSync(join(installRoot, "source"), { recursive: true });
    writeFileSync(join(installRoot, "source", "version.txt"), "old");
    writeExecutable(join(installRoot, "venv", "bin", "hugagent"), "#!/bin/bash\nexit 0\n");
    writeFileSync(join(installRoot, "installed-bundle.json"), '{"desktop_version":"old"}\n');
    writeExecutable(
      join(installRoot, "tools", "uv"),
      `#!/bin/bash
set -e
if [[ "$1" == "--system-certs" ]]; then shift; fi
printf '%s\n' "$*" >> "\${HUGAGENT_UV_LOG:?}"
if [[ "$1" == "venv" ]]; then
  destination="\${!#}"
  mkdir -p "$destination/bin"
  printf '#!/bin/bash\\nexit 0\\n' > "$destination/bin/python"
  chmod +x "$destination/bin/python"
elif [[ "$1" == "pip" && " $* " == *" --requirements "* ]]; then
  exit 9
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
        env: {
          ...process.env,
          HUGAGENT_SKIP_OPTIONAL_NODE: "1",
          HUGAGENT_UV_LOG: uvLog,
        },
      },
    );

    assert.equal(result.status, 9, result.stderr || result.stdout);
    assert.equal(readFileSync(join(installRoot, "source", "version.txt"), "utf8"), "old");
    assert.equal(
      readFileSync(join(installRoot, "installed-bundle.json"), "utf8"),
      '{"desktop_version":"old"}\n',
    );
  } finally {
    rmSync(fixture, { recursive: true, force: true });
  }
});

test("macOS bootstrap pins verified uv downloads and checks free space", () => {
  const contents = readFileSync(installer, "utf8");
  assert.match(contents, /--retry 5/);
  assert.match(contents, /--retry-all-errors/);
  assert.match(contents, /shasum -a 256/);
  assert.match(contents, /UvSha256="9bed3567/);
  assert.match(contents, /HUGAGENT_MIN_FREE_KB/);
});

test("macOS bootstrap stops before copying when free space is insufficient", () => {
  const fixture = mkdtempSync(join(tmpdir(), "hugagent-macos-disk-"));
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
        env: { ...process.env, HUGAGENT_MIN_FREE_KB: "999999999999" },
      },
    );

    assert.equal(result.status, 4, result.stderr || result.stdout);
    assert.match(result.stderr, /Not enough disk space/);
    assert.equal(existsSync(join(installRoot, "source")), false);
  } finally {
    rmSync(fixture, { recursive: true, force: true });
  }
});
