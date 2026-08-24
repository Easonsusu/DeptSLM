import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  developmentAuthorization,
  normalizeEnvironment,
} from "../lib/dev-auth.mjs";

const webRoot = fileURLToPath(new URL("..", import.meta.url));

test("development bridge adds a bearer only in explicitly local environments", () => {
  const token = "signed-local-token-not-for-logs";
  assert.equal(normalizeEnvironment(" DEVELOPMENT "), "development");
  assert.equal(
    developmentAuthorization({ environment: "development", token }),
    `Bearer ${token}`,
  );
  assert.equal(
    developmentAuthorization({ environment: "production", token }),
    undefined,
  );
  assert.equal(
    developmentAuthorization({ environment: "unknown", token }),
    undefined,
  );
});

test("development bridge preserves an existing caller Authorization header", () => {
  assert.equal(
    developmentAuthorization({
      environment: "production",
      token: "server-token",
      existingAuthorization: "Bearer caller-token",
    }),
    "Bearer caller-token",
  );
});

test("blank, malformed, or Bearer-prefixed bridge values fail closed", () => {
  for (const token of ["", "   ", "has whitespace", "Bearer already-prefixed"]) {
    assert.equal(
      developmentAuthorization({ environment: "test", token }),
      undefined,
    );
  }
});

test("bridge source has no browser secret or logging path", () => {
  const middleware = readFileSync(`${webRoot}/middleware.js`, "utf8");
  const helper = readFileSync(`${webRoot}/lib/dev-auth.mjs`, "utf8");
  assert.doesNotMatch(`${middleware}\n${helper}`, /NEXT_PUBLIC_|localStorage|console\.|logger\./);
  assert.match(middleware, /requestHeaders\.set\("authorization"/);
  assert.match(middleware, /matcher: \["\/api\/:path\*"\]/);
});

test("API proxy destination is fixed server configuration, not request-controlled", async () => {
  const config = await import("../next.config.mjs");
  assert.equal(config.resolveApiUrl("http://api:8000/"), "http://api:8000");
  assert.throws(() => config.resolveApiUrl("http://api:8000?target=https://evil.invalid"));
  assert.throws(() => config.resolveApiUrl("javascript:alert(1)"));
  const previous = process.env.API_URL;
  process.env.API_URL = "http://api:8000";
  try {
    assert.deepEqual(await config.default.rewrites(), [
      { source: "/api/:path*", destination: "http://api:8000/:path*" },
    ]);
  } finally {
    if (previous === undefined) delete process.env.API_URL;
    else process.env.API_URL = previous;
  }
});
