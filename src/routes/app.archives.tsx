import { createFileRoute } from "@tanstack/react-router";
import { FileLibraryPage } from "@/components/pv/FileLibraryPage";

export const Route = createFileRoute("/app/archives")({
  component: () => (
    <FileLibraryPage
      apiPath="archives"
      title="Archives"
      description="mixed preserved collections"
      emptyTitle="Archives is empty"
      emptyDescription="Move preserved collections from the Arrival Hall into Archives to display them here."
    />
  ),
});
