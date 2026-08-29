export type AuthSession = {
  authenticated: boolean;
  username: string | null;
  display_name: string | null;
  role: "administrator" | "user" | null;
  password_change_required: boolean;
};

export async function getAuthSession(): Promise<AuthSession> {
  const response = await fetch("/api/auth/session", {
    method: "GET",
    credentials: "include",
    headers: {
      Accept: "application/json",
    },
  });

  if (!response.ok) {
    throw new Error("Unable to check authentication session");
  }

  return response.json() as Promise<AuthSession>;
}
