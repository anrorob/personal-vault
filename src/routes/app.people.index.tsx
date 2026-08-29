import { Link, createFileRoute, useNavigate } from "@tanstack/react-router";
import { Plus, Search, UserRound } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { PersonForm } from "@/components/pv/PersonForm";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  emptyPersonDraft,
  galleryProfileAssets,
  peopleRequest,
  personPayload,
  profileImageCropStyle,
  type GalleryProfileAsset,
  type Person,
} from "@/lib/people";

export const Route = createFileRoute("/app/people/")({ component: PeoplePage });

function PeoplePage() {
  const navigate = useNavigate();
  const [people, setPeople] = useState<Person[] | null>(null);
  const [query, setQuery] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [profileAssets, setProfileAssets] = useState<GalleryProfileAsset[]>([]);

  const load = useCallback(
    async (search = "") => {
      setError(null);
      try {
        const suffix = search.trim() ? `?query=${encodeURIComponent(search.trim())}` : "";
        setPeople(await peopleRequest<Person[]>(suffix));
      } catch (requestError) {
        if ((requestError as Error & { status?: number }).status === 401)
          await navigate({ to: "/login" });
        else setError("People could not be loaded. Please try again.");
      }
    },
    [navigate],
  );

  useEffect(() => {
    void load(query);
  }, [load, query]);
  useEffect(() => {
    void galleryProfileAssets()
      .then(setProfileAssets)
      .catch(() => undefined);
  }, []);

  const create = async (draft: ReturnType<typeof emptyPersonDraft>) => {
    setSaving(true);
    try {
      const created = await peopleRequest<Person>("", {
        method: "POST",
        body: JSON.stringify(personPayload(draft)),
      });
      setCreateOpen(false);
      await navigate({ to: "/app/people/$personId", params: { personId: created.person_id } });
    } catch (requestError) {
      setError(
        (requestError as Error & { status?: number }).status === 409
          ? "Mark a Person as Me before assigning a relationship."
          : "This Person could not be created. Check the fields and try again.",
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <section className="flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
        <div>
          <h2 className="pv-content-title text-2xl md:text-3xl">People</h2>
          <p className="mt-1 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            The people who matter in your Vault.
          </p>
        </div>
        <button
          className="pv-btn-primary inline-flex items-center justify-center gap-2 px-4 py-2.5 text-sm"
          onClick={() => setCreateOpen(true)}
        >
          <Plus size={17} /> Add Person
        </button>
      </section>
      <label className="relative block max-w-xl">
        <Search
          className="absolute left-3 top-1/2 -translate-y-1/2"
          size={17}
          style={{ color: "var(--pv-text-dim)" }}
        />
        <input
          className="pv-input w-full pl-10"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search names, preferred names, or aliases"
          aria-label="Search People"
        />
      </label>
      {error && <div className="pv-panel pv-status-error p-6 text-center text-sm">{error}</div>}
      {!error && people === null && (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
          {Array.from({ length: 5 }, (_, index) => (
            <div key={index} className="pv-panel aspect-[3/4] animate-pulse" />
          ))}
        </div>
      )}
      {!error && people?.length === 0 && (
        <div className="pv-panel flex flex-col items-center px-6 py-16 text-center">
          <UserRound size={30} style={{ color: "var(--pv-gold)" }} />
          <h3 className="mt-4 text-lg" style={{ color: "var(--pv-silver)" }}>
            {query ? "No People match that search" : "No People yet"}
          </h3>
          <p className="mt-2 max-w-md text-sm" style={{ color: "var(--pv-text-dim)" }}>
            {query
              ? "Try a full name, preferred name, or alias."
              : "Add someone to begin building your People directory."}
          </p>
          {!query && (
            <button
              className="pv-btn-secondary mt-5 px-4 py-2 text-sm"
              onClick={() => setCreateOpen(true)}
            >
              Add your first Person
            </button>
          )}
        </div>
      )}
      {!error && people && people.length > 0 && (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
          {people.map((person) => (
            <PersonCard
              key={person.person_id}
              person={person}
              profile={profileAssets.find((asset) => asset.asset_id === person.profile_asset_id)}
            />
          ))}
        </div>
      )}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent
          className="max-h-[90vh] max-w-2xl overflow-y-auto"
          style={{ background: "var(--pv-panel-2)", borderColor: "var(--pv-border-strong)" }}
        >
          <DialogHeader>
            <DialogTitle style={{ color: "var(--pv-gold-bright)" }}>Add Person</DialogTitle>
            <DialogDescription>
              Keep the directory personal, clear, and easy to browse.
            </DialogDescription>
          </DialogHeader>
          <PersonForm
            submitLabel="Create Person"
            busy={saving}
            onSubmit={create}
            onCancel={() => setCreateOpen(false)}
          />
        </DialogContent>
      </Dialog>
    </div>
  );
}

function PersonCard({ person, profile }: { person: Person; profile?: GalleryProfileAsset }) {
  return (
    <Link
      to="/app/people/$personId"
      params={{ personId: person.person_id }}
      className="pv-panel pv-panel-hover group flex aspect-[3/4] min-h-0 flex-col overflow-hidden text-left"
      aria-label={`Open ${person.full_name}`}
    >
      <div
        className="relative aspect-square shrink-0 overflow-hidden"
        style={{ background: "linear-gradient(145deg, #23232a, #101014)" }}
      >
        {profile ? (
          <img
            src={profile.thumbnail_url}
            alt={`${person.full_name} profile`}
            className="absolute max-w-none object-cover"
            style={profileImageCropStyle(person.profile_frame)}
          />
        ) : (
          <div className="flex h-full items-center justify-center">
            <UserRound size={38} style={{ color: "var(--pv-silver-dim)" }} />
          </div>
        )}
        {person.is_me && (
          <span
            className="absolute left-2 top-2 rounded-full border px-2 py-1 text-[10px]"
            style={{
              color: "var(--pv-gold-bright)",
              borderColor: "var(--pv-gold)",
              background: "rgba(10,10,12,.82)",
            }}
          >
            Me
          </span>
        )}
      </div>
      <div className="flex min-h-0 flex-1 flex-col justify-center p-3">
        <h3 className="line-clamp-2 text-sm leading-5" style={{ color: "var(--pv-silver)" }}>
          {person.full_name}
        </h3>
        {person.preferred_name && (
          <p className="mt-1 truncate text-xs" style={{ color: "var(--pv-text-dim)" }}>
            {person.preferred_name}
          </p>
        )}
        {person.relationship_label && (
          <p className="mt-1 truncate text-xs" style={{ color: "var(--pv-gold)" }}>
            {person.relationship_label}
          </p>
        )}
      </div>
    </Link>
  );
}
