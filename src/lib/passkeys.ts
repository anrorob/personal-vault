type PublicKeyOptionsResponse = {
  challenge_id: string;
  publicKey: Record<string, unknown>;
};

function fromBase64url(value: string): Uint8Array {
  const padded = value
    .replace(/-/g, "+")
    .replace(/_/g, "/")
    .padEnd(Math.ceil(value.length / 4) * 4, "=");
  const binary = window.atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function toBase64url(value: ArrayBuffer | null): string | null {
  if (!value) return null;
  const binary = String.fromCharCode(...new Uint8Array(value));
  return window.btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function creationOptions(options: Record<string, unknown>): PublicKeyCredentialCreationOptions {
  const user = options.user as Record<string, unknown>;
  const excludeCredentials = Array.isArray(options.excludeCredentials)
    ? options.excludeCredentials.map((item) => ({
        ...(item as PublicKeyCredentialDescriptor),
        id: fromBase64url(String((item as Record<string, unknown>).id)),
      }))
    : undefined;
  return {
    ...(options as unknown as PublicKeyCredentialCreationOptions),
    challenge: fromBase64url(String(options.challenge)),
    user: {
      ...(user as unknown as PublicKeyCredentialUserEntity),
      id: fromBase64url(String(user.id)),
    },
    excludeCredentials,
  };
}

function requestOptions(options: Record<string, unknown>): PublicKeyCredentialRequestOptions {
  const allowCredentials = Array.isArray(options.allowCredentials)
    ? options.allowCredentials.map((item) => ({
        ...(item as PublicKeyCredentialDescriptor),
        id: fromBase64url(String((item as Record<string, unknown>).id)),
      }))
    : undefined;
  return {
    ...(options as unknown as PublicKeyCredentialRequestOptions),
    challenge: fromBase64url(String(options.challenge)),
    allowCredentials,
  };
}

function registrationJson(credential: PublicKeyCredential): Record<string, unknown> {
  const response = credential.response as AuthenticatorAttestationResponse;
  return {
    id: credential.id,
    rawId: toBase64url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    response: {
      clientDataJSON: toBase64url(response.clientDataJSON),
      attestationObject: toBase64url(response.attestationObject),
      transports: typeof response.getTransports === "function" ? response.getTransports() : [],
    },
    clientExtensionResults: credential.getClientExtensionResults(),
  };
}

function authenticationJson(credential: PublicKeyCredential): Record<string, unknown> {
  const response = credential.response as AuthenticatorAssertionResponse;
  return {
    id: credential.id,
    rawId: toBase64url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    response: {
      clientDataJSON: toBase64url(response.clientDataJSON),
      authenticatorData: toBase64url(response.authenticatorData),
      signature: toBase64url(response.signature),
      userHandle: toBase64url(response.userHandle),
    },
    clientExtensionResults: credential.getClientExtensionResults(),
  };
}

async function options(
  path: string,
  body?: Record<string, unknown>,
): Promise<PublicKeyOptionsResponse> {
  const response = await fetch(path, {
    method: "POST",
    credentials: "include",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok)
    throw new Error(
      response.status === 429
        ? "Too many attempts. Try again later."
        : "Passkeys are not available right now.",
    );
  return response.json() as Promise<PublicKeyOptionsResponse>;
}

export function passkeysSupported(): boolean {
  return (
    typeof window !== "undefined" && "PublicKeyCredential" in window && !!navigator.credentials
  );
}

export async function registerPasskey(label?: string): Promise<void> {
  if (!passkeysSupported()) throw new Error("This browser does not support passkeys.");
  const start = await options("/api/auth/passkeys/registration/options");
  const created = await navigator.credentials.create({
    publicKey: creationOptions(start.publicKey),
  });
  if (!(created instanceof PublicKeyCredential))
    throw new Error("Passkey registration was cancelled.");
  const response = await fetch("/api/auth/passkeys/registration/verify", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      challenge_id: start.challenge_id,
      credential: registrationJson(created),
      label,
    }),
  });
  if (!response.ok) throw new Error("The passkey could not be verified.");
}

export async function registerEnrolmentPasskey(token: string, label?: string): Promise<void> {
  if (!passkeysSupported()) throw new Error("This browser does not support passkeys.");
  const start = await options("/api/auth/enrolment/registration/options", { token });
  const created = await navigator.credentials.create({
    publicKey: creationOptions(start.publicKey),
  });
  if (!(created instanceof PublicKeyCredential))
    throw new Error("Passkey registration was cancelled.");
  const response = await fetch("/api/auth/enrolment/registration/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      token,
      challenge_id: start.challenge_id,
      credential: registrationJson(created),
      label,
    }),
  });
  if (!response.ok)
    throw new Error("The enrolment link is invalid or the passkey could not be verified.");
}

export async function registerRecoveryPasskey(token: string, label?: string): Promise<void> {
  if (!passkeysSupported()) throw new Error("This browser does not support passkeys.");
  const start = await options("/api/auth/recovery/registration/options", { token });
  const created = await navigator.credentials.create({
    publicKey: creationOptions(start.publicKey),
  });
  if (!(created instanceof PublicKeyCredential))
    throw new Error("Passkey registration was cancelled.");
  const response = await fetch("/api/auth/recovery/registration/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      token,
      challenge_id: start.challenge_id,
      credential: registrationJson(created),
      label,
    }),
  });
  if (!response.ok)
    throw new Error("The recovery link is invalid or the passkey could not be verified.");
}

export async function authenticateWithPasskey(): Promise<{ password_change_required?: boolean }> {
  if (!passkeysSupported()) throw new Error("This browser does not support passkeys.");
  const start = await options("/api/auth/passkeys/authentication/options");
  const assertion = await navigator.credentials.get({ publicKey: requestOptions(start.publicKey) });
  if (!(assertion instanceof PublicKeyCredential))
    throw new Error("Passkey sign-in was cancelled.");
  const response = await fetch("/api/auth/passkeys/authentication/verify", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      challenge_id: start.challenge_id,
      credential: authenticationJson(assertion),
    }),
  });
  if (!response.ok)
    throw new Error(
      response.status === 429
        ? "Too many passkey attempts. Try again later."
        : "Passkey sign-in failed. You can still use your password.",
    );
  return response.json() as Promise<{ password_change_required?: boolean }>;
}

export async function elevateVaultControl(): Promise<void> {
  if (!passkeysSupported()) throw new Error("This browser does not support passkeys.");
  const start = await options("/api/auth/vault-control/elevation/options");
  const assertion = await navigator.credentials.get({ publicKey: requestOptions(start.publicKey) });
  if (!(assertion instanceof PublicKeyCredential))
    throw new Error("Vault Control identity confirmation was cancelled.");
  const response = await fetch("/api/auth/vault-control/elevation/verify", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      challenge_id: start.challenge_id,
      credential: authenticationJson(assertion),
    }),
  });
  if (!response.ok) throw new Error("Vault Control identity confirmation failed.");
}
