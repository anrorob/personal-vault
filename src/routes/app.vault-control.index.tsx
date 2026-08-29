import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/app/vault-control/")({
  beforeLoad: () => {
    throw redirect({ to: "/app/vault-control/overview" });
  },
});
