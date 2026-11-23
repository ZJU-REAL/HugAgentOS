import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { buildDesktopRuntime } from "./build-runtime.mjs";
import { findPython } from "./process-utils.mjs";

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(desktopDir, "..");
const sourceRoot = join(desktopDir, "generated", "server-ce");
const python = findPython("archive the offline runtime");

buildDesktopRuntime({ desktopDir, repoRoot, sourceRoot, python });
