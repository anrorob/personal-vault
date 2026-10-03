import { afterEach, expect, test } from "bun:test";
import { authorizeHiddenPhotos } from "../src/lib/passkeys";
const original = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = original;
});
test("Hidden authorization is checked freshly, never cached across sessions", async () => {
  let authorized = true;
  let calls = 0;
  globalThis.fetch = (async (url, options) => {
    expect(String(url)).toBe("/api/auth/hidden-photos/authorization");
    expect(options?.cache).toBe("no-store");
    expect(options?.credentials).toBe("include");
    calls++;
    return Response.json({ authorized });
  }) as typeof fetch;
  await authorizeHiddenPhotos();
  authorized = false;
  // Synthetic environment has no passkey support: a new session must not reuse approval.
  await expect(authorizeHiddenPhotos()).rejects.toThrow();
  expect(calls).toBe(2);
});
test("Hidden status errors fail closed before starting a ceremony", async () => {
  globalThis.fetch = (async () => Response.json({}, { status: 401 })) as typeof fetch;
  await expect(authorizeHiddenPhotos()).rejects.toThrow(
    "Hidden Photos session could not be verified.",
  );
});
