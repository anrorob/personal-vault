import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/app/incoming")({
  beforeLoad: () => {
    throw redirect({ to: "/app/arrival-hall", replace: true });
  },
});
