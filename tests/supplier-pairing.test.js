import { afterEach, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import {
  copyPairingCredential,
  generatePairingCredential,
  pairingCredentialExpired,
} from "../src/lib/supplier-pairing";

const originalFetch = globalThis.fetch;
const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, "navigator");
afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalNavigator) Object.defineProperty(globalThis, "navigator", originalNavigator);
  else delete globalThis.navigator;
});

const credential = "PVPAIR1.eyJ2IjoxfQ";
const expiresAt = () => new Date(Date.now() + 600_000).toISOString();

test("generates through the authenticated API and copies the complete single value", async () => {
  globalThis.fetch = async (url, options) => {
    expect(url).toBe("/api/vault-supplier/pairing-code");
    expect(options).toEqual({ method: "POST", credentials: "include" });
    return Response.json({ pairing_credential: credential, expires_at: expiresAt() });
  };
  const value = await generatePairingCredential();
  let copied;
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: {
      clipboard: {
        writeText: async (text) => {
          copied = text;
        },
      },
    },
  });
  await copyPairingCredential(value);
  expect(copied).toBe(credential);
});

test("rejects legacy, malformed, and expired issuance responses", async () => {
  for (const body of [
    { pairing_code: "OLD_SHORT_CODE", expires_at: expiresAt() },
    { pairing_credential: "OLD_SHORT_CODE", expires_at: expiresAt() },
    { pairing_credential: credential, expires_at: "invalid" },
    { pairing_credential: credential, expires_at: "2000-01-01T00:00:00Z" },
  ]) {
    globalThis.fetch = async () => Response.json(body);
    await expect(generatePairingCredential()).rejects.toThrow();
  }
});

test("shows domain error messages and prevents copying expired credentials", async () => {
  globalThis.fetch = async () =>
    Response.json(
      { detail: { code: "invalid_pairing_origin", message: "Invalid pairing origin." } },
      { status: 400 },
    );
  await expect(generatePairingCredential()).rejects.toThrow("Invalid pairing origin.");
  const expired = { code: credential, expiresAt: "2000-01-01T00:00:00Z" };
  expect(pairingCredentialExpired(expired)).toBe(true);
  await expect(copyPairingCredential(expired)).rejects.toThrow("expired");
});

test("Security UI wires the generate, display, copy, and expiry actions", () => {
  const source = readFileSync(new URL("../src/routes/app.security.tsx", import.meta.url), "utf8");
  expect(source).toContain("Generate pairing credential");
  expect(source).toContain("await generatePairingCredential()");
  expect(source).toContain('aria-label="Pairing credential"');
  expect(source).toContain("value={pairingCode.code}");
  expect(source).toContain("copyPairingCredential(pairingCode)");
  expect(source).toContain("window.clearTimeout(timeout)");
  expect(source).not.toContain("Generate pairing code");
});
