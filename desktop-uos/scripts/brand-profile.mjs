const MODES = new Set(["local_only", "cloud_only", "dual"]);

function normalizedBrand(value) {
  return String(value || "")
    .toLowerCase()
    .replaceAll(/[^a-z0-9]/g, "");
}

export function resolveDefaultProvisionMode({ brandName, flavor, override = "" }) {
  const requested = String(override || "").trim().toLowerCase();
  if (requested) {
    if (!MODES.has(requested)) {
      throw new Error(`JX_DEFAULT_PROVISION_MODE must be one of ${[...MODES].join(", ")}`);
    }
    return requested;
  }

  if (flavor === "thin") return "cloud_only";
  return normalizedBrand(brandName) === "hugagentagent" ? "dual" : "local_only";
}
