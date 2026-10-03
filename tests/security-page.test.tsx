import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { createRootRoute, createRouter, createMemoryHistory, RouterProvider } =
  await import("@tanstack/react-router");
const { Route: SecurityRoute } = await import("../src/routes/app.security");
const originalFetch = globalThis.fetch;
const originalConfirm = window.confirm;
const originalCredential = Object.getOwnPropertyDescriptor(window, "PublicKeyCredential");
const originalCredentials = Object.getOwnPropertyDescriptor(navigator, "credentials");
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
let requests: { url: string; method: string }[];
const date = (day: number) => `2026-09-${String(day).padStart(2, "0")}T12:00:00Z`;

async function mount(count = 6) {
  Object.defineProperty(window, "PublicKeyCredential", { configurable: true, value: class {} });
  Object.defineProperty(navigator, "credentials", { configurable: true, value: {} });
  requests = [];
  // Intentionally unsorted; creation order differs from revocation order.
  const events = [2, 6, 1, 5, 3, 4].slice(0, count).map((day) => ({
    id: `event-${day}`,
    event_type: "signed_out",
    occurred_at: date(day),
    user_agent: `Synthetic browser event-${day}`,
    client_ip: null,
    authentication_method: null,
  }));
  const installations = Array.from({ length: count }, (_, index) => ({
    installation_id: `active-${index}-uuid`,
    supplier_version: `Active version ${index}`,
    protocol_version: 1,
    created_at: date(index + 1),
    last_seen_at: date(12),
    revoked_at: null as string | null,
  }));
  for (const day of [2, 6, 1, 5, 3, 4].slice(0, count))
    installations.push({
      installation_id: `revoked-${day}-uuid`,
      supplier_version: `Revoked version ${day}`,
      protocol_version: 1,
      created_at: date(1),
      last_seen_at: date(1),
      revoked_at: date(day),
    });
  globalThis.fetch = (async (input, options) => {
    const url = String(input);
    requests.push({ url, method: options?.method ?? "GET" });
    if (options?.method === "DELETE") {
      const installation = installations.find((item) => url.endsWith(item.installation_id));
      if (installation) installation.revoked_at = date(12);
      return Response.json({ status: "revoked" });
    }
    if (url === "/api/auth/passkeys")
      return Response.json(
        count
          ? [
              {
                id: "passkey-uuid",
                label: "Synthetic passkey",
                created_at: date(1),
                last_used_at: date(12),
                authenticator_attachment: "platform",
              },
            ]
          : [],
      );
    if (url === "/api/auth/session") return Response.json({ password_login_enabled: true });
    if (url === "/api/auth/security-events") return Response.json(events);
    if (url === "/api/vault-supplier/installations") return Response.json(installations);
    return Response.json([]);
  }) as typeof fetch;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  const parent = createRootRoute();
  const route = SecurityRoute.update({
    getParentRoute: () => parent,
    path: "/app/security",
  } as never);
  const router = createRouter({
    routeTree: parent.addChildren([route]),
    history: createMemoryHistory({ initialEntries: ["/app/security"] }),
  });
  await act(async () => {
    await router.load();
    root.render(<RouterProvider router={router} />);
  });
  await flush();
  return { events, installations };
}
async function flush() {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 30));
  });
}
function section(name: string) {
  return container.querySelector<HTMLElement>(`section[aria-labelledby="${name}-heading"]`)!;
}
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  container?.remove();
  globalThis.fetch = originalFetch;
  window.confirm = originalConfirm;
  if (originalCredential) Object.defineProperty(window, "PublicKeyCredential", originalCredential);
  else Reflect.deleteProperty(window, "PublicKeyCredential");
  if (originalCredentials) Object.defineProperty(navigator, "credentials", originalCredentials);
  else Reflect.deleteProperty(navigator, "credentials");
});

test("real Security page groups one passkey action and its registered list together", async () => {
  await mount();
  const passkeys = section("passkeys");
  expect(passkeys.querySelector("h2")?.textContent).toBe("Passkeys");
  expect(passkeys.textContent).toContain("Synthetic passkey");
  expect(passkeys.textContent).toContain("last used");
  const actions = [...container.querySelectorAll("button")].filter(
    (b) => b.textContent === "Add passkey",
  );
  expect(actions).toHaveLength(1);
  expect(passkeys.contains(actions[0])).toBe(true);
  expect(passkeys.className).toContain("p-4 sm:p-6");
  expect(passkeys.querySelector(".border-t")?.className).toContain("flex-wrap");
  expect(section("supplier").querySelector(".border-t")?.className).toContain("flex-wrap");
});

test("all active Supplier pairings remain revocable; only newest three revoked records render", async () => {
  const { installations } = await mount();
  const supplier = section("supplier");
  expect(supplier.querySelectorAll("button.pv-btn-danger")).toHaveLength(6);
  for (let i = 0; i < 6; i++) expect(supplier.textContent).toContain(`Active version ${i}`);
  const history = [...supplier.querySelectorAll(".border-t")].filter((el) =>
    el.textContent?.includes("Revoked version"),
  );
  expect(history.map((el) => el.textContent?.match(/Revoked version \d/)?.[0])).toEqual([
    "Revoked version 6",
    "Revoked version 5",
    "Revoked version 4",
  ]);
  expect(installations).toHaveLength(12);
  expect(requests.every((r) => r.method === "GET")).toBe(true);
  window.confirm = () => false;
  await act(async () => supplier.querySelector<HTMLButtonElement>("button.pv-btn-danger")!.click());
  expect(requests.every((r) => r.method === "GET")).toBe(true);
  window.confirm = () => true;
  await act(async () => supplier.querySelector<HTMLButtonElement>("button.pv-btn-danger")!.click());
  await flush();
  expect(requests.filter((r) => r.method === "DELETE")).toEqual([
    { url: "/api/vault-supplier/installations/active-0-uuid", method: "DELETE" },
  ]);
  expect(section("supplier").querySelectorAll("button.pv-btn-danger")).toHaveLength(5);
});

test("activity shows three newest events without mutating or deleting audit data", async () => {
  const { events } = await mount();
  const activity = section("activity");
  expect(
    [...activity.querySelectorAll(".border-t")].map((el) => el.textContent?.match(/event-\d/)?.[0]),
  ).toEqual(["event-6", "event-5", "event-4"]);
  expect(events.map((event) => event.id)).toEqual([
    "event-2",
    "event-6",
    "event-1",
    "event-5",
    "event-3",
    "event-4",
  ]);
  expect(requests.every((r) => r.method === "GET")).toBe(true);
  expect(activity.querySelector(".border-t")?.className).toContain("break-words");
});

test("empty and short histories render normally", async () => {
  await mount(0);
  expect(section("passkeys").textContent).toContain("No passkeys have been added yet.");
  expect(section("supplier").textContent).toContain("No Vault Supplier installations");
  expect(section("activity").textContent).toContain("No recent security activity.");
});

test("fewer than three events and historical pairings are retained", async () => {
  await mount(2);
  expect(section("activity").querySelectorAll(".border-t")).toHaveLength(2);
  expect(section("supplier").querySelectorAll(".border-t")).toHaveLength(4);
  expect(section("supplier").querySelectorAll("button.pv-btn-danger")).toHaveLength(2);
});
