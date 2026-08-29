import { createFileRoute, redirect } from "@tanstack/react-router";

import { getAuthSession } from "@/lib/auth";

export const Route = createFileRoute("/")({
  ssr: false,
  beforeLoad: async () => {
    const session = await getAuthSession();

    throw redirect({
      to: session.authenticated ? "/app" : "/login",
    });
  },
});
