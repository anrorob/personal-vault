import { createFileRoute } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";

export const Route = createFileRoute("/app/vault-control/federation")({
  component: FederationControlPage,
});

type Incoming = {
  incoming_share_id: string;
  owner_label: string;
  display_title: string;
  asset_type: string;
  state: string;
};
type IncomingCollection = {
  incoming_collection_id: string;
  owner_label: string;
  name: string;
  member_count: number;
  state: string;
};
type LocalUser = { user_id: string; display_name: string; active: boolean };

function FederationControlPage() {
  const [incoming, setIncoming] = useState<Incoming[]>([]);
  const [incomingCollections, setIncomingCollections] = useState<IncomingCollection[]>([]);
  const [users, setUsers] = useState<LocalUser[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    const [shares, collections, accounts] = await Promise.all([
      fetch("/api/vault-master/federation/incoming/admin", { credentials: "include" }),
      fetch("/api/vault-master/federation/incoming-collections/admin", { credentials: "include" }),
      fetch("/api/vault-control/users", { credentials: "include" }),
    ]);
    if (!shares.ok || !collections.ok || !accounts.ok)
      throw new Error("Incoming Vault shares could not be loaded.");
    setIncoming(((await shares.json()) as { shares: Incoming[] }).shares);
    setIncomingCollections(
      ((await collections.json()) as { collections: IncomingCollection[] }).collections,
    );
    setUsers(
      ((await accounts.json()) as { users: Array<LocalUser & { user_id?: string }> }).users.filter(
        (user): user is LocalUser => Boolean(user.user_id) && user.active,
      ),
    );
  }, []);
  useEffect(
    () =>
      void load().catch((reason: unknown) =>
        setError(reason instanceof Error ? reason.message : "Unavailable"),
      ),
    [load],
  );
  const distribute = async (
    shareId: string,
    mode: "everyone" | "specific",
    recipientIds: string[] = [],
  ) => {
    const response = await fetch(`/api/vault-master/federation/incoming/${shareId}/distribution`, {
      method: "PUT",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, recipient_user_ids: recipientIds }),
    });
    if (!response.ok) {
      setError("Local distribution could not be saved.");
      return;
    }
    await load();
  };
  const removeDistribution = async (shareId: string) => {
    const response = await fetch(`/api/vault-master/federation/incoming/${shareId}/distribution`, {
      method: "DELETE",
      credentials: "include",
    });
    if (!response.ok) {
      setError("Local distribution could not be removed.");
      return;
    }
    await load();
  };
  const distributeCollection = async (
    collectionId: string,
    mode: "everyone" | "specific",
    recipientIds: string[] = [],
  ) => {
    const response = await fetch(
      `/api/vault-master/federation/incoming-collections/${collectionId}/distribution`,
      {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, recipient_user_ids: recipientIds }),
      },
    );
    if (!response.ok) {
      setError("Collection visibility could not be saved.");
      return;
    }
    await load();
  };
  const removeCollectionDistribution = async (collectionId: string) => {
    const response = await fetch(
      `/api/vault-master/federation/incoming-collections/${collectionId}/distribution`,
      { method: "DELETE", credentials: "include" },
    );
    if (!response.ok) {
      setError("Collection visibility could not be removed.");
      return;
    }
    await load();
  };
  return (
    <section className="mx-auto max-w-5xl space-y-5">
      <div>
        <h2 className="pv-page-title">Incoming Vault Shares</h2>
        <p className="mt-1 text-sm" style={{ color: "var(--pv-text-dim)" }}>
          Incoming content remains owned by its origin Vault. Choose local visibility without
          exposing this Vault’s user directory.
        </p>
      </div>
      {error && <p className="text-sm text-red-300">{error}</p>}
      {incoming.length === 0 ? (
        <div className="pv-panel p-5 text-sm" style={{ color: "var(--pv-text-dim)" }}>
          No incoming federated shares.
        </div>
      ) : (
        incoming.map((share) => (
          <article key={share.incoming_share_id} className="pv-panel space-y-3 p-4">
            <div>
              <h3 style={{ color: "var(--pv-silver)" }}>{share.display_title}</h3>
              <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                From {share.owner_label} · {share.asset_type} · {share.state}
              </p>
            </div>
            {share.state === "active" && (
              <div className="flex flex-wrap gap-2">
                <button
                  className="pv-btn-primary px-3 py-2 text-xs"
                  onClick={() => void distribute(share.incoming_share_id, "everyone")}
                >
                  Everyone in this Vault
                </button>
                {users.map((user) => (
                  <button
                    key={user.user_id}
                    className="pv-btn-ghost px-3 py-2 text-xs"
                    onClick={() =>
                      void distribute(share.incoming_share_id, "specific", [user.user_id])
                    }
                  >
                    {user.display_name}
                  </button>
                ))}
                <button
                  className="pv-btn-ghost px-3 py-2 text-xs"
                  onClick={() => void removeDistribution(share.incoming_share_id)}
                >
                  Remove local visibility
                </button>
              </div>
            )}
          </article>
        ))
      )}
      {incomingCollections.length > 0 && (
        <section className="space-y-3">
          <h3 className="text-sm" style={{ color: "var(--pv-silver)" }}>
            Incoming Vault Collections
          </h3>
          {incomingCollections.map((collection) => (
            <article key={collection.incoming_collection_id} className="pv-panel space-y-3 p-4">
              <div>
                <h3 style={{ color: "var(--pv-silver)" }}>{collection.name}</h3>
                <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  From {collection.owner_label} · {collection.member_count} items ·{" "}
                  {collection.state}
                </p>
              </div>
              {collection.state === "active" && (
                <div className="flex flex-wrap gap-2">
                  <button
                    className="pv-btn-primary px-3 py-2 text-xs"
                    onClick={() =>
                      void distributeCollection(collection.incoming_collection_id, "everyone")
                    }
                  >
                    Everyone in this Vault
                  </button>
                  {users.map((user) => (
                    <button
                      key={user.user_id}
                      className="pv-btn-ghost px-3 py-2 text-xs"
                      onClick={() =>
                        void distributeCollection(collection.incoming_collection_id, "specific", [
                          user.user_id,
                        ])
                      }
                    >
                      {user.display_name}
                    </button>
                  ))}
                  <button
                    className="pv-btn-ghost px-3 py-2 text-xs"
                    onClick={() =>
                      void removeCollectionDistribution(collection.incoming_collection_id)
                    }
                  >
                    Remove local visibility
                  </button>
                </div>
              )}
            </article>
          ))}
        </section>
      )}
    </section>
  );
}
