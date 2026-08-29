import { createFileRoute, Outlet } from "@tanstack/react-router";

export const Route = createFileRoute("/app/gallery")({
  component: GalleryLayout,
});

function GalleryLayout() {
  return <Outlet />;
}
