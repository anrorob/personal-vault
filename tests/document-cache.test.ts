import { expect, test } from "bun:test";
import { documentCachePolicy } from "../src/lib/document-cache";

for (const path of [
  "/",
  "/login",
  "/app",
  "/app/arrival-hall",
  "/app/music",
  "/unknown/fallback?view=1",
]) {
  for (const method of ["GET", "HEAD"]) {
    test(`${method} ${path} HTML cannot retain stale bundle references`, async () => {
      const original = new Response(
        method === "HEAD" ? null : '<script src="/assets/example-new.js"></script>',
        {
          headers: {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "public, max-age=3600",
            Vary: "Accept-Encoding",
            "X-Example": "preserved",
          },
        },
      );
      const result = documentCachePolicy(
        new Request(`https://vault.invalid${path}`, { method }),
        original,
      );
      expect(result.status).toBe(200);
      expect(result.headers.get("cache-control")).toBe("no-store");
      expect(result.headers.get("vary")).toBe("Accept-Encoding");
      expect(result.headers.get("x-example")).toBe("preserved");
      if (method === "GET") expect(await result.text()).toContain("example-new.js");
    });
  }
}

for (const [path, type, cache] of [
  ["/assets/example-abc123.js", "text/javascript", "public, max-age=31536000, immutable"],
  ["/assets/example-def456.css", "text/css", "public, max-age=31536000, immutable"],
  ["/api/health", "application/json", "no-cache"],
  ["/api/auth/session", "application/json", "no-store"],
  ["/api/download", "application/octet-stream", "private"],
  ["/media/example", "video/mp4", "private, max-age=60"],
  ["/thumbnail/example", "image/webp", "public, max-age=600"],
  ["/build-info.json", "application/json", "max-age=0"],
]) {
  test(`${path} keeps its own cache policy`, () => {
    const response = new Response("synthetic", {
      headers: { "Content-Type": type, "Cache-Control": cache },
    });
    const result = documentCachePolicy(new Request(`https://vault.invalid${path}`), response);
    expect(result).toBe(response);
    expect(result.headers.get("cache-control")).toBe(cache);
  });
}

test("HTML error documents also avoid storage; API and asset errors stay untouched", () => {
  for (const path of ["/app/music", "/api/example", "/assets/missing.js"]) {
    const response = new Response("error", {
      status: 500,
      headers: { "Content-Type": "text/html" },
    });
    const result = documentCachePolicy(new Request(`https://vault.invalid${path}`), response);
    expect(result.status).toBe(500);
    expect(result.headers.get("cache-control")).toBe(path === "/app/music" ? "no-store" : null);
  }
});

test("POST and responses without HTML content type are unchanged", () => {
  const html = new Response("html", { headers: { "Content-Type": "text/html" } });
  expect(
    documentCachePolicy(new Request("https://vault.invalid/app", { method: "POST" }), html),
  ).toBe(html);
  const unknown = new Response(null);
  expect(documentCachePolicy(new Request("https://vault.invalid/app"), unknown)).toBe(unknown);
});
