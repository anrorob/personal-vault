import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";

type Category = "gallery" | "theatre" | "home-videos" | "documents" | "library";
type SharedAsset = {
  asset_id: string;
  asset_type: string;
  display_title: string;
  captured_on: string | null;
  owner_display_name: string;
  preview_url: string | null;
  content_url?: string | null;
  cache_state?: string | null;
  download_allowed?: boolean;
  is_federated?: boolean;
  origin_metadata?: {
    description?: string | null;
    tags?: string[];
    location?: string | null;
    people?: string[];
    collection_context?: string | null;
  } | null;
};
type SharedCollection = {
  collection_id: string;
  name: string;
  description: string | null;
  owner_display_name: string;
  member_count: number;
  is_federated?: boolean;
  state?: string;
};

const categories: { id: Category; label: string }[] = [
  { id: "gallery", label: "Gallery" },
  { id: "theatre", label: "Theatre" },
  { id: "home-videos", label: "Home Videos" },
  { id: "documents", label: "Documents & Archives" },
  { id: "library", label: "Library" },
];

export const Route = createFileRoute("/app/commons")({ component: VaultCommonsPage });

function SharedAssetCard({
  asset,
  onOpen,
}: {
  asset: SharedAsset;
  onOpen: (asset: SharedAsset) => void;
}) {
  const cache = async () => {
    if (!asset.is_federated) return;
    const confirmed = window.confirm(
      "Keep a managed local cache? It remains shared, is not yours, and may disappear if the owner revokes access.",
    );
    if (!confirmed) return;
    await fetch(`/api/vault-master/federation/incoming/${asset.asset_id}/cache`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm_large_media: true }),
    });
  };
  const download = async () => {
    if (!asset.is_federated || !asset.download_allowed) return;
    const confirmed = window.confirm(
      "Download to My Vault creates a new copy owned by you. It remains if the original share is later revoked.",
    );
    if (!confirmed) return;
    await fetch(`/api/vault-master/federation/incoming/${asset.asset_id}/download`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idempotency_key: crypto.randomUUID() }),
    });
  };
  if (asset.asset_type.toLowerCase() === "gallery") {
    return (
      <button
        type="button"
        className="group relative aspect-square overflow-hidden rounded-md text-left"
        onClick={() => onOpen(asset)}
      >
        {asset.preview_url ? (
          <img
            src={asset.preview_url}
            alt={asset.display_title}
            className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-105"
          />
        ) : (
          <span
            className="flex h-full items-center justify-center bg-black text-xs"
            style={{ color: "var(--pv-text-dim)" }}
          >
            Preview unavailable
          </span>
        )}
        <span
          className="absolute inset-x-0 bottom-0 bg-black/65 px-2 py-1 text-[11px]"
          style={{ color: "var(--pv-silver)" }}
        >
          Shared by {asset.owner_display_name}
        </span>
      </button>
    );
  }
  const media = [
    "movie",
    "movies",
    "personal videos",
    "personal video",
    "home videos",
    "home video",
  ].includes(asset.asset_type.toLowerCase());
  return (
    <article className="pv-panel overflow-hidden p-4">
      {asset.preview_url && (
        <img
          src={asset.preview_url}
          alt=""
          className="mb-3 h-36 w-full rounded-md object-cover"
          onError={(event) => {
            event.currentTarget.style.display = "none";
          }}
        />
      )}
      <h3 className="text-sm" style={{ color: "var(--pv-silver)" }}>
        {asset.display_title}
      </h3>
      <p className="mt-2 text-xs" style={{ color: "var(--pv-text-dim)" }}>
        Shared by {asset.owner_display_name}
      </p>
      {asset.captured_on && (
        <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
          {asset.captured_on}
        </p>
      )}
      {media && asset.content_url && (
        <div className="mt-3 flex gap-2">
          <button className="pv-btn-primary px-3 py-2 text-xs" onClick={() => onOpen(asset)}>
            Play
          </button>
          {asset.is_federated && (
            <button className="pv-btn-ghost px-3 py-2 text-xs" onClick={() => void cache()}>
              {asset.cache_state === "complete" ? "Cached" : "Cache locally"}
            </button>
          )}
          {asset.is_federated && asset.download_allowed && (
            <button className="pv-btn-ghost px-3 py-2 text-xs" onClick={() => void download()}>
              Download to My Vault
            </button>
          )}
        </div>
      )}
      {media && (
        <p className="mt-2 text-xs" style={{ color: "var(--pv-text-dim)" }}>
          {asset.cache_state === "complete" ? "Cached" : "Remote"}
        </p>
      )}
    </article>
  );
}

function VaultCommonsPage() {
  const [category, setCategory] = useState<Category>("gallery");
  const [assets, setAssets] = useState<SharedAsset[] | null>(null);
  const [collections, setCollections] = useState<SharedCollection[] | null>(null);
  const [openedCollection, setOpenedCollection] = useState<SharedCollection | null>(null);
  const [collectionAssets, setCollectionAssets] = useState<SharedAsset[] | null>(null);
  const [openedAsset, setOpenedAsset] = useState<SharedAsset | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setAssets(null);
    void fetch(`/api/vault-master/commons/shared-with-me?category=${category}`, {
      credentials: "include",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error("Vault Commons could not be loaded.");
        setAssets(((await response.json()) as { assets: SharedAsset[] }).assets);
      })
      .catch(() => !controller.signal.aborted && setAssets([]));
    void fetch("/api/vault-master/commons/shared-collections", {
      credentials: "include",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error("Shared collections could not be loaded.");
        setCollections(
          ((await response.json()) as { collections: SharedCollection[] }).collections,
        );
      })
      .catch(() => !controller.signal.aborted && setCollections([]));
    return () => controller.abort();
  }, [category]);
  useEffect(() => {
    if (!openedCollection) return;
    const controller = new AbortController();
    setCollectionAssets(null);
    void fetch(
      `/api/vault-master/commons/shared-collections/${openedCollection.collection_id}/members`,
      {
        credentials: "include",
        signal: controller.signal,
      },
    )
      .then(async (response) => {
        if (!response.ok) throw new Error();
        setCollectionAssets(((await response.json()) as { assets: SharedAsset[] }).assets);
      })
      .catch(() => !controller.signal.aborted && setCollectionAssets([]));
    return () => controller.abort();
  }, [openedCollection]);
  return (
    <section className="mx-auto max-w-6xl space-y-5">
      <div>
        <h2 className="pv-page-title">Vault Commons</h2>
        <p className="mt-1 text-sm" style={{ color: "var(--pv-text-dim)" }}>
          Content others have shared with you, kept separate from your own Vault.
        </p>
      </div>
      <div className="flex gap-2 border-b pb-3" style={{ borderColor: "var(--pv-border)" }}>
        <span className="pv-btn-primary px-3 py-2 text-xs">Shared with me</span>
        <Link to="/app/files-i-shared" className="pv-btn-ghost px-3 py-2 text-xs">
          Files I Shared
        </Link>
      </div>

      <>
        {collections && collections.length > 0 && (
          <section className="space-y-2">
            <h3 className="text-sm" style={{ color: "var(--pv-silver)" }}>
              Collections shared with me
            </h3>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {collections.map((collection) => (
                <article key={collection.collection_id} className="pv-panel p-4">
                  <h4 className="text-sm" style={{ color: "var(--pv-silver)" }}>
                    {collection.name}
                  </h4>
                  <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                    Shared by {collection.owner_display_name} · {collection.member_count} items
                    {collection.is_federated ? " · From another Vault" : ""}
                  </p>
                  {collection.description && (
                    <p className="mt-2 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                      {collection.description}
                    </p>
                  )}
                  <button
                    className="mt-3 text-xs"
                    style={{ color: "var(--pv-gold)" }}
                    onClick={() => setOpenedCollection(collection)}
                  >
                    Open collection
                  </button>
                </article>
              ))}
            </div>
          </section>
        )}
        {openedCollection && (
          <section className="pv-panel space-y-3 p-4">
            <div className="flex justify-between gap-3">
              <h3 className="text-sm" style={{ color: "var(--pv-silver)" }}>
                {openedCollection.name}
              </h3>
              <button
                className="text-xs"
                style={{ color: "var(--pv-gold)" }}
                onClick={() => setOpenedCollection(null)}
              >
                Close
              </button>
            </div>
            {collectionAssets === null ? (
              <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                Opening collection…
              </p>
            ) : (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
                {collectionAssets.map((asset) => (
                  <SharedAssetCard key={asset.asset_id} asset={asset} onOpen={setOpenedAsset} />
                ))}
              </div>
            )}
          </section>
        )}
        <div className="flex flex-wrap gap-2">
          {categories.map((item) => (
            <button
              key={item.id}
              className={
                category === item.id
                  ? "pv-btn-primary px-3 py-2 text-xs"
                  : "pv-btn-ghost px-3 py-2 text-xs"
              }
              onClick={() => setCategory(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
        {assets === null ? (
          <p className="text-sm" style={{ color: "var(--pv-text-dim)" }}>
            Opening shared content…
          </p>
        ) : assets.length === 0 ? (
          <div className="pv-panel p-8 text-center text-sm" style={{ color: "var(--pv-text-dim)" }}>
            Nothing has been shared with you in{" "}
            {categories.find((item) => item.id === category)?.label} yet.
          </div>
        ) : (
          <div
            className={
              category === "gallery"
                ? "grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4"
                : "grid gap-3 sm:grid-cols-2 lg:grid-cols-3"
            }
          >
            {assets.map((asset) => (
              <SharedAssetCard key={asset.asset_id} asset={asset} onOpen={setOpenedAsset} />
            ))}
          </div>
        )}
      </>
      {openedAsset && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 p-4"
          role="dialog"
          aria-modal="true"
        >
          <div
            className="max-h-full max-w-5xl overflow-auto rounded-md bg-black p-4"
            style={{ border: "1px solid var(--pv-border)" }}
          >
            <div className="mb-3 flex items-start justify-between gap-4">
              <div>
                <h3 style={{ color: "var(--pv-silver)" }}>{openedAsset.display_title}</h3>
                <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  Shared by {openedAsset.owner_display_name}
                  {openedAsset.captured_on ? ` · ${openedAsset.captured_on}` : ""}
                </p>
                {openedAsset.origin_metadata?.location && (
                  <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                    {openedAsset.origin_metadata.location}
                  </p>
                )}
              </div>
              <button
                className="pv-btn-ghost px-3 py-2 text-xs"
                onClick={() => setOpenedAsset(null)}
              >
                Close
              </button>
            </div>
            {openedAsset.content_url ? (
              <video
                className="max-h-[75vh] w-full"
                controls
                autoPlay
                src={openedAsset.content_url}
              />
            ) : (
              openedAsset.preview_url && (
                <img
                  src={openedAsset.preview_url}
                  alt={openedAsset.display_title}
                  className="max-h-[75vh] w-auto max-w-full object-contain"
                />
              )
            )}
            {openedAsset.origin_metadata?.description && (
              <p className="mt-3 max-w-3xl text-sm" style={{ color: "var(--pv-text-dim)" }}>
                {openedAsset.origin_metadata.description}
              </p>
            )}
            {openedAsset.is_federated && openedAsset.download_allowed && (
              <button
                className="mt-3 pv-btn-ghost px-3 py-2 text-xs"
                onClick={() => {
                  if (
                    !window.confirm(
                      "Download to My Vault creates a new copy owned by you. It remains if the original share is later revoked.",
                    )
                  )
                    return;
                  void fetch(
                    `/api/vault-master/federation/incoming/${openedAsset.asset_id}/download`,
                    {
                      method: "POST",
                      credentials: "include",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ idempotency_key: crypto.randomUUID() }),
                    },
                  );
                }}
              >
                Download to My Vault
              </button>
            )}
            {((openedAsset.origin_metadata?.tags?.length ?? 0) > 0 ||
              (openedAsset.origin_metadata?.people?.length ?? 0) > 0 ||
              openedAsset.origin_metadata?.collection_context) && (
              <p className="mt-2 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                {openedAsset.origin_metadata?.tags?.join(" · ")}
                {openedAsset.origin_metadata?.people?.length
                  ? `${openedAsset.origin_metadata?.tags?.length ? " · " : ""}${openedAsset.origin_metadata.people.join(", ")}`
                  : ""}
                {openedAsset.origin_metadata?.collection_context
                  ? ` · ${openedAsset.origin_metadata.collection_context}`
                  : ""}
              </p>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
