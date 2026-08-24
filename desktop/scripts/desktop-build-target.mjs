const NATIVE_TARGETS = {
  "win32:x64": "x86_64-pc-windows-msvc",
  "darwin:arm64": "aarch64-apple-darwin",
  "darwin:x64": "x86_64-apple-darwin",
  "linux:x64": "x86_64-unknown-linux-gnu",
  "linux:arm64": "aarch64-unknown-linux-gnu",
};

export function explicitTauriTarget(args) {
  const equals = args.find((argument) => argument.startsWith("--target="));
  if (equals) return equals.slice("--target=".length);
  const index = args.indexOf("--target");
  if (index === -1) return null;
  if (!args[index + 1] || args[index + 1].startsWith("-")) {
    throw new Error("--target requires a Rust target triple.");
  }
  return args[index + 1];
}

export function validateDesktopBuildTarget(
  args,
  platform = process.platform,
  arch = process.arch,
) {
  const native = NATIVE_TARGETS[`${platform}:${arch}`];
  if (!native) {
    throw new Error(`Unsupported desktop release builder: ${platform}/${arch}`);
  }
  const requested = explicitTauriTarget(args);
  if (requested && requested !== native) {
    throw new Error(
      `The offline runtime is native to ${native}; cannot package it for ${requested}. ` +
        "Build a separate installer on the matching OS and CPU architecture.",
    );
  }
  return native;
}
