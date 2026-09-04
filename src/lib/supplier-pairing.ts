export type PairingCredential = { code: string; expiresAt: string };

export async function generatePairingCredential(): Promise<PairingCredential> {
  const response = await fetch("/api/vault-supplier/pairing-code", {
    method: "POST",
    credentials: "include",
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(body?.detail?.message ?? "Could not generate a pairing credential.");
  }
  if (
    typeof body?.pairing_credential !== "string" ||
    !/^PVPAIR1\.[A-Za-z0-9_-]+$/.test(body.pairing_credential) ||
    typeof body.expires_at !== "string" ||
    !Number.isFinite(Date.parse(body.expires_at))
  ) {
    throw new Error("The Vault returned an invalid pairing credential.");
  }
  const result = { code: body.pairing_credential, expiresAt: body.expires_at };
  if (pairingCredentialExpired(result)) throw new Error("The pairing credential has expired.");
  return result;
}

export function pairingCredentialExpired(credential: PairingCredential): boolean {
  return Date.parse(credential.expiresAt) <= Date.now();
}

export async function copyPairingCredential(credential: PairingCredential): Promise<void> {
  if (pairingCredentialExpired(credential)) throw new Error("The pairing credential has expired.");
  await navigator.clipboard.writeText(credential.code);
}
