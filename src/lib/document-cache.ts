/** HTML is a deployment manifest; never retain references to removed bundles. */
export function documentCachePolicy(request: Request, response: Response): Response {
  const path = new URL(request.url).pathname;
  const type = response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
  if (
    !["GET", "HEAD"].includes(request.method) ||
    path === "/api" ||
    path.startsWith("/api/") ||
    path === "/assets" ||
    path.startsWith("/assets/") ||
    type !== "text/html"
  )
    return response;

  const headers = new Headers(response.headers);
  headers.set("Cache-Control", "no-store");
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}
