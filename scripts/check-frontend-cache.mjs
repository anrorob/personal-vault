import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { setTimeout as delay } from "node:timers/promises";

const reservation = createServer();
await new Promise((resolve) => reservation.listen(0, "127.0.0.1", resolve));
const port = reservation.address().port;
await new Promise((resolve) => reservation.close(resolve));
const base = `http://127.0.0.1:${port}`;
const server = spawn(process.execPath, [".output/server/index.mjs"], {
  env: { ...process.env, HOST: "127.0.0.1", PORT: String(port) },
  windowsHide: true,
  stdio: ["ignore", "pipe", "pipe"],
});
let diagnostic = "";
server.stdout.on("data", (data) => (diagnostic += data));
server.stderr.on("data", (data) => (diagnostic += data));
try {
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    try {
      await fetch(base);
      ready = true;
      break;
    } catch {
      if (server.exitCode !== null) break;
      await delay(100);
    }
  }
  assert.ok(ready, `Built frontend did not start: ${diagnostic}`);
  let html = "";
  for (const path of ["/", "/login", "/app", "/app/arrival-hall", "/app/music"]) {
    for (const method of ["GET", "HEAD"]) {
      const response = await fetch(base + path, { method });
      assert.equal(response.status, 200, `${method} ${path}`);
      assert.match(response.headers.get("content-type"), /^text\/html/);
      assert.equal(response.headers.get("cache-control"), "no-store");
      html += await response.text();
    }
  }
  const assets = [...new Set(html.match(/\/assets\/[^"'<>\s]+\.(?:js|css)/g))];
  assert.ok(assets.some((path) => path.endsWith(".js")));
  assert.ok(assets.some((path) => path.endsWith(".css")));
  for (const path of assets) {
    const response = await fetch(base + path);
    assert.equal(response.status, 200, path);
    assert.equal(response.headers.get("cache-control"), "public, max-age=31536000, immutable");
    await response.arrayBuffer();
  }
  const info = await fetch(base + "/build-info.json");
  assert.equal(info.status, 200);
  assert.match(info.headers.get("content-type"), /application\/json/);
  assert.notEqual(info.headers.get("cache-control"), "no-store");
  console.log(
    `PASS: five document routes GET/HEAD no-store; ${assets.length} immutable assets; JSON unchanged.`,
  );
} finally {
  if (server.exitCode === null) {
    const stopped = new Promise((resolve) => server.once("exit", resolve));
    server.kill();
    await stopped;
  }
}
