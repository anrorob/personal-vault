import { createFileRoute, redirect } from "@tanstack/react-router";
import { getAuthSession } from "@/lib/auth";
import { AppShell } from "@/components/pv/AppShell";

export const Route = createFileRoute("/app")({
  ssr: false,
  beforeLoad: async () => {
    const session = await getAuthSession();

    if (!session.authenticated) {
      throw redirect({
        to: "/login",
      });
    }
    if (session.password_change_required) {
      throw redirect({ to: "/change-password" });
    }
  },
  head: () => ({
    meta: [{ title: "Personal Vault" }, { name: "robots", content: "noindex" }],
  }),
  component: AppShell,
});
