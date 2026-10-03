import { createFileRoute, Outlet, useLocation } from "@tanstack/react-router";
import { useEffect } from "react";
import { clearGalleryPosition, visitGalleryPath } from "@/lib/gallery";

export const Route = createFileRoute("/app/gallery")({
  component: GalleryLayout,
});

function GalleryLayout() {
  const { pathname } = useLocation();
  visitGalleryPath(pathname);
  useEffect(() => clearGalleryPosition, []);
  return <Outlet />;
}
