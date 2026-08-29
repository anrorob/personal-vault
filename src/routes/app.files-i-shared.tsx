import { createFileRoute, Link } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";

type Recipient = { display_name: string };
type Operation = {
  operation_id: string;
  share_mode: "quick" | "standard";
  state: "pending" | "active" | "revoked";
  release_at: string | null;
  subject_type: "assets" | "collection";
  collection: { name: string; member_count: number; owner_display_name: string } | null;
  collection_members: { asset_id: string; asset_title: string }[];
  grants: {
    asset_id: string;
    asset_title: string;
    preview_url: string | null;
    target_type: string;
    recipient: Recipient | null;
  }[];
};

export const Route = createFileRoute("/app/files-i-shared")({ component: FilesISharedPage });

function FilesISharedPage() {
  const [operations, setOperations] = useState<Operation[]>([]);
  const [federated, setFederated] = useState<
    Array<{
      federation_share_id: string;
      display_title: string;
      target_label: string;
      share_mode: string;
      state: string;
      release_at: string | null;
      preview_url: string | null;
      download_allowed: boolean;
    }>
  >([]);
  const [federatedCollections, setFederatedCollections] = useState<
    Array<{
      federation_collection_share_id: string;
      name: string;
      member_count: number;
      target_label: string;
      share_mode: string;
      state: string;
      release_at: string | null;
    }>
  >([]);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    const response = await fetch("/api/vault-master/sharing/outgoing", { credentials: "include" });
    if (!response.ok) throw new Error("Files I Shared could not be loaded.");
    setOperations(((await response.json()) as { operations: Operation[] }).operations);
    const [remote, remoteCollections] = await Promise.all([
      fetch("/api/vault-master/federation/outgoing", { credentials: "include" }),
      fetch("/api/vault-master/federation/outgoing-collections", { credentials: "include" }),
    ]);
    if (remote.ok) setFederated((await remote.json()) as typeof federated);
    if (remoteCollections.ok)
      setFederatedCollections((await remoteCollections.json()) as typeof federatedCollections);
  }, []);
  useEffect(() => {
    void load().catch((reason: unknown) =>
      setError(reason instanceof Error ? reason.message : "Files I Shared could not be loaded."),
    );
  }, [load]);
  const transition = async (operationId: string, action: "share-now" | "revoke") => {
    setError(null);
    const response = await fetch(`/api/vault-master/sharing/outgoing/${operationId}/${action}`, {
      method: "POST",
      credentials: "include",
    });
    if (!response.ok) {
      setError(((await response.json()) as { detail?: string }).detail ?? "Share update failed.");
      return;
    }
    await load();
  };
  const transitionFederated = async (shareId: string, action: "share-now" | "revoke") => {
    const response = await fetch(`/api/vault-master/federation/outgoing/${shareId}/${action}`, {
      method: "POST",
      credentials: "include",
    });
    if (!response.ok) {
      setError("Federated share update failed.");
      return;
    }
    await load();
  };
  const transitionFederatedCollection = async (shareId: string, action: "share-now" | "revoke") => {
    const response = await fetch(
      `/api/vault-master/federation/outgoing-collections/${shareId}/${action}`,
      {
        method: "POST",
        credentials: "include",
      },
    );
    if (!response.ok) {
      setError("Federated collection update failed.");
      return;
    }
    await load();
  };
  const setDownloadPermission = async (shareId: string, downloadAllowed: boolean) => {
    setError(null);
    const response = await fetch(
      `/api/vault-master/federation/outgoing/${shareId}/download-permission`,
      {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ download_allowed: downloadAllowed }),
      },
    );
    if (!response.ok) {
      setError("Download permission could not be updated.");
      return;
    }
    await load();
  };
  return (
    <section className="mx-auto max-w-4xl space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="pv-page-title">Files I Shared</h2>
          <p className="mt-1 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            Review the actual outgoing share records. Sharing never moves or copies Vault files.
          </p>
        </div>
        <Link to="/app/commons" className="pv-btn-ghost px-3 py-2 text-xs">
          Back to Vault Commons
        </Link>
      </div>
      {error && (
        <p className="text-sm" style={{ color: "#fca5a5" }}>
          {error}
        </p>
      )}
      {operations.length === 0 ? (
        <p
          className="rounded-md p-4 text-sm"
          style={{ border: "1px solid var(--pv-border)", color: "var(--pv-text-dim)" }}
        >
          You have not shared any files yet.
        </p>
      ) : (
        operations.map((operation) => (
          <article
            key={operation.operation_id}
            className="space-y-3 rounded-md p-4"
            style={{ border: "1px solid var(--pv-border)" }}
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <span className="text-sm" style={{ color: "var(--pv-silver)" }}>
                  {operation.subject_type === "collection" && operation.collection
                    ? `${operation.collection.name} · ${operation.collection.member_count} items`
                    : operation.share_mode === "standard"
                      ? "Standard Share"
                      : "Quick Share"}
                </span>
                <span className="ml-2 text-xs uppercase" style={{ color: "var(--pv-gold)" }}>
                  {operation.state}
                </span>
                {operation.state === "pending" && operation.release_at && (
                  <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                    Automatically releases at {new Date(operation.release_at).toLocaleTimeString()}.
                  </p>
                )}
              </div>
              <div className="flex gap-2">
                {operation.state === "pending" && (
                  <button
                    className="pv-btn-primary px-3 py-2 text-xs"
                    onClick={() => void transition(operation.operation_id, "share-now")}
                  >
                    Share now
                  </button>
                )}
                {(operation.state === "pending" || operation.state === "active") && (
                  <button
                    className="rounded-md px-3 py-2 text-xs"
                    style={{ border: "1px solid var(--pv-border)", color: "var(--pv-silver)" }}
                    onClick={() => void transition(operation.operation_id, "revoke")}
                  >
                    {operation.state === "pending" ? "Cancel" : "Revoke"}
                  </button>
                )}
              </div>
            </div>
            <ul className="space-y-2">
              {operation.subject_type === "collection" && (
                <li className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  Stored collection members
                  <ul className="mt-1 space-y-1">
                    {operation.collection_members.map((member) => (
                      <li key={member.asset_id}>{member.asset_title}</li>
                    ))}
                  </ul>
                </li>
              )}
              {operation.grants.map((grant) => (
                <li
                  key={`${grant.asset_id}-${grant.recipient?.display_name ?? grant.target_type}`}
                  className="flex justify-between gap-3 text-sm"
                >
                  <span className="flex items-center gap-2" style={{ color: "var(--pv-silver)" }}>
                    {grant.preview_url && (
                      <img
                        src={grant.preview_url}
                        alt=""
                        className="h-10 w-10 rounded object-cover"
                      />
                    )}
                    {grant.asset_title}
                  </span>
                  <span style={{ color: "var(--pv-text-dim)" }}>
                    {grant.target_type === "local_all"
                      ? "Everyone in this Vault"
                      : grant.recipient?.display_name}
                  </span>
                </li>
              ))}
            </ul>
          </article>
        ))
      )}
      {federated.length > 0 && (
        <section className="space-y-3">
          <h3 className="text-sm" style={{ color: "var(--pv-silver)" }}>
            Shared with another Vault
          </h3>
          {federated.map((share) => (
            <article
              key={share.federation_share_id}
              className="flex flex-wrap items-center justify-between gap-3 rounded-md p-4"
              style={{ border: "1px solid var(--pv-border)" }}
            >
              <span className="flex items-center gap-2" style={{ color: "var(--pv-silver)" }}>
                {share.preview_url && (
                  <img src={share.preview_url} alt="" className="h-10 w-10 rounded object-cover" />
                )}
                {share.display_title}{" "}
                <span className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  → {share.target_label} · {share.state}
                </span>
              </span>
              <div className="flex gap-2">
                {share.state === "active" && (
                  <button
                    className="pv-btn-ghost px-3 py-2 text-xs"
                    onClick={() =>
                      void setDownloadPermission(share.federation_share_id, !share.download_allowed)
                    }
                  >
                    {share.download_allowed ? "Downloads allowed" : "Allow Download to My Vault"}
                  </button>
                )}
                {share.state === "pending" && (
                  <button
                    className="pv-btn-primary px-3 py-2 text-xs"
                    onClick={() => void transitionFederated(share.federation_share_id, "share-now")}
                  >
                    Share now
                  </button>
                )}
                {(share.state === "pending" || share.state === "active") && (
                  <button
                    className="pv-btn-ghost px-3 py-2 text-xs"
                    onClick={() => void transitionFederated(share.federation_share_id, "revoke")}
                  >
                    {share.state === "pending" ? "Cancel" : "Revoke"}
                  </button>
                )}
              </div>
            </article>
          ))}
        </section>
      )}
      {federatedCollections.length > 0 && (
        <section className="space-y-3">
          <h3 className="text-sm" style={{ color: "var(--pv-silver)" }}>
            Collections shared with another Vault
          </h3>
          {federatedCollections.map((share) => (
            <article
              key={share.federation_collection_share_id}
              className="flex flex-wrap items-center justify-between gap-3 rounded-md p-4"
              style={{ border: "1px solid var(--pv-border)" }}
            >
              <span style={{ color: "var(--pv-silver)" }}>
                {share.name} · {share.member_count} items
                <span className="ml-2 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  → {share.target_label} · {share.state}
                </span>
              </span>
              <div className="flex gap-2">
                {share.state === "pending" && (
                  <button
                    className="pv-btn-primary px-3 py-2 text-xs"
                    onClick={() =>
                      void transitionFederatedCollection(
                        share.federation_collection_share_id,
                        "share-now",
                      )
                    }
                  >
                    Share now
                  </button>
                )}
                {(share.state === "pending" || share.state === "active") && (
                  <button
                    className="pv-btn-ghost px-3 py-2 text-xs"
                    onClick={() =>
                      void transitionFederatedCollection(
                        share.federation_collection_share_id,
                        "revoke",
                      )
                    }
                  >
                    {share.state === "pending" ? "Cancel" : "Revoke"}
                  </button>
                )}
              </div>
            </article>
          ))}
        </section>
      )}
    </section>
  );
}
