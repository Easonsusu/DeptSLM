const LOCAL_ENVIRONMENTS = new Set(["local", "development", "test"]);

export function normalizeEnvironment(value) {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

/**
 * Return the only Authorization value the local bridge may add.
 *
 * This module is server-only by convention: it is imported by middleware and
 * is never imported by a client component. It deliberately has no logging or
 * error formatting path so the bearer value cannot be copied into diagnostics.
 */
export function developmentAuthorization({ environment, token, existingAuthorization }) {
  if (typeof existingAuthorization === "string" && existingAuthorization.trim()) {
    return existingAuthorization;
  }
  if (!LOCAL_ENVIRONMENTS.has(normalizeEnvironment(environment))) {
    return undefined;
  }
  const value = typeof token === "string" ? token.trim() : "";
  if (!value || /\s/.test(value) || /^Bearer\s/i.test(value)) {
    return undefined;
  }
  return `Bearer ${value}`;
}
