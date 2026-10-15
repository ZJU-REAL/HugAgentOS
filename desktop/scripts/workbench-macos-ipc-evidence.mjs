export const nativeIpcProbes = [
  "logout_desktop", "open_login", "plugin:event|emit", "plugin:event|listen",
  "plugin:window|set_title", "plugin:window|set_size", "plugin:window|close",
  "plugin:window|start_dragging",
];
export function emptyNativeIpcProof() {
  return { transport: "", installedPackageSha256: "", hostSessionSentinel: "", hostWindowSentinel: "",
    sessionUnchanged: false, windowUnchanged: false, eventDeliveryCount: null,
    probes: ["srcdoc", "external-iframe"].flatMap(frame => nativeIpcProbes.map(command => ({
      frame, command, status: "not-run", validPayload: false, denialKind: "", ipcResponseCode: "", ipcResponse: "",
    }))), hostRegressions: { logout: "not-run", login: "not-run", menus: "not-run",
      updater: "not-run", windowControls: "not-run", dragging: "not-run" } };
}
export function validateNativeIpcProof(proof) {
  if (!proof || proof.transport !== "actual-package-ipc" ||
      !/^[a-f0-9]{64}$/.test(proof.installedPackageSha256 || "") ||
      !proof.hostSessionSentinel || !proof.hostWindowSentinel ||
      proof.sessionUnchanged !== true || proof.windowUnchanged !== true || proof.eventDeliveryCount !== 0 ||
      proof.probes?.length !== 2 * nativeIpcProbes.length) {
    throw new Error("Missing actual package IPC response and unchanged host sentinels.");
  }
  for (const expected of emptyNativeIpcProof().probes) {
    const actual = proof.probes.find(probe => probe.frame === expected.frame && probe.command === expected.command);
    if (!actual || actual.status !== "denied" || actual.validPayload !== true ||
        !(["logout_desktop", "open_login"].includes(actual.command)
          ? ["removed", "permission"].includes(actual.denialKind) : actual.denialKind === "permission") ||
        !actual.ipcResponseCode?.trim() || !actual.ipcResponse?.trim()) {
      throw new Error("Native IPC attack not denied: " + expected.frame + "/" + expected.command);
    }
  }
  for (const action of Object.keys(emptyNativeIpcProof().hostRegressions)) {
    if (proof.hostRegressions?.[action] !== "pass") throw new Error("Host regression not passed: " + action);
  }
}
