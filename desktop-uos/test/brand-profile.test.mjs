import assert from "node:assert/strict";
import test from "node:test";

import { resolveDefaultProvisionMode } from "../scripts/brand-profile.mjs";

test("full HugAgentOS builds default to dual mode", () => {
  assert.equal(resolveDefaultProvisionMode({ brandName: "HugAgentOS", flavor: "full" }), "dual");
  assert.equal(resolveDefaultProvisionMode({ brandName: "HugAgentOS Agent", flavor: "full" }), "dual");
});

test("other full builds keep local-first and thin builds stay cloud-only", () => {
  assert.equal(resolveDefaultProvisionMode({ brandName: "HugAgentOS", flavor: "full" }), "local_only");
  assert.equal(resolveDefaultProvisionMode({ brandName: "HugAgentOS", flavor: "thin" }), "cloud_only");
});

test("an explicit build override wins and invalid values fail early", () => {
  assert.equal(resolveDefaultProvisionMode({ brandName: "HugAgentOS", flavor: "full", override: "dual" }), "dual");
  assert.throws(
    () => resolveDefaultProvisionMode({ brandName: "HugAgentOS", flavor: "full", override: "mixed" }),
    /JX_DEFAULT_PROVISION_MODE/,
  );
});
