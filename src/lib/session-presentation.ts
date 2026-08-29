export type SessionPresentation = {
  user_agent: string | null;
  authentication_method: string | null;
  created_at: string;
  last_seen_at: string | null;
  expires_at: string;
  vault_control_elevated: boolean;
};

function browser(userAgent: string): string | null {
  if (/edg(?:e|a|ios)?\//i.test(userAgent)) return "Microsoft Edge";
  if (/firefox\//i.test(userAgent) || /fxios\//i.test(userAgent)) return "Firefox";
  if (/crios\//i.test(userAgent) || /chrome\//i.test(userAgent)) return "Chrome";
  if (/safari\//i.test(userAgent)) return "Safari";
  return null;
}

function platform(userAgent: string): string | null {
  if (/iphone/i.test(userAgent)) return "iPhone";
  if (/ipad/i.test(userAgent)) return "iPad";
  if (/android/i.test(userAgent)) return "Android";
  if (/windows nt/i.test(userAgent)) return "Windows";
  if (/macintosh|mac os x/i.test(userAgent)) return "Mac";
  if (/linux/i.test(userAgent)) return "Linux";
  return null;
}

export function describeSessionClient(userAgent: string | null): string {
  if (!userAgent) return "Unknown browser";
  const browserName = browser(userAgent);
  const platformName = platform(userAgent);
  if (browserName && platformName) return `${browserName} on ${platformName}`;
  return browserName ?? platformName ?? "Unknown browser";
}

export function describeAuthenticationMethod(method: string | null): string {
  if (method === "passkey") return "Signed in with passkey";
  if (method === "password") return "Signed in with password";
  return "Sign-in method unavailable";
}

export function relativeExpiry(value: string, now = Date.now()): string {
  const minutes = Math.max(0, Math.ceil((new Date(value).getTime() - now) / 60_000));
  if (minutes < 60) return `Expires in ${minutes}m`;
  return `Expires in ${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function relativeActivity(value: string | null, current: boolean, now = Date.now()): string {
  if (current) return "Active now";
  if (!value) return "Activity unavailable";
  const minutes = Math.max(0, Math.floor((now - new Date(value).getTime()) / 60_000));
  if (minutes < 5) return "Active now";
  if (minutes < 60) return `Last active ${minutes} min ago`;
  return `Last active ${Math.floor(minutes / 60)} h ago`;
}
