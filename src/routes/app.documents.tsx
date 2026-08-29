import { createFileRoute } from "@tanstack/react-router";
import { FileLibraryPage } from "@/components/pv/FileLibraryPage";

export const Route = createFileRoute("/app/documents")({
  component: () => (
    <FileLibraryPage
      apiPath="documents"
      title="Documents"
      description="document-focused records"
      emptyTitle="Documents is empty"
      emptyDescription="Move staged documents from the Arrival Hall into the Documents library to display them here."
    />
  ),
});
