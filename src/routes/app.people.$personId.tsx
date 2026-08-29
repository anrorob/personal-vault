import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { ArrowLeft, Image as ImageIcon, Pencil, Star, UserRound } from "lucide-react";
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
  galleryProfileAssets,
  peopleRequest,
  personDraftFrom,
  personPayload,
  profileImageCropStyle,
  type GalleryProfileAsset,
  type PersonDetail,
  type Person,
  type PersonDraft,
} from "@/lib/people";

export const Route = createFileRoute("/app/people/$personId")({ component: PersonDetailPage });

function PersonDetailPage() {
  const { personId } = Route.useParams();
  const navigate = useNavigate();
  const [person, setPerson] = useState<PersonDetail | null>(null);
  const [profileAssets, setProfileAssets] = useState<GalleryProfileAsset[]>([]);
  const [editing, setEditing] = useState(false);
  const [mergeOpen, setMergeOpen] = useState(false);
  const [correctionOpen, setCorrectionOpen] = useState(false);
  const [allPeople, setAllPeople] = useState<Person[]>([]);
  const [mergeSourceId, setMergeSourceId] = useState("");
  const [correctionAssetId, setCorrectionAssetId] = useState("");
  const [correctionPersonId, setCorrectionPersonId] = useState("");
  const [newPersonName, setNewPersonName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    setError(null);
    setPerson(null);
    try {
      setPerson(await peopleRequest<PersonDetail>(`/${personId}`));
    } catch (requestError) {
      const status = (requestError as Error & { status?: number }).status;
      if (status === 401) await navigate({ to: "/login" });
      else if (status === 404) setError("This Person is unavailable or you do not have access.");
      else setError("This Person could not be loaded.");
    }
  }, [navigate, personId]);
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    void galleryProfileAssets()
      .then(setProfileAssets)
      .catch(() => undefined);
  }, []);
  useEffect(() => {
    void peopleRequest<Person[]>("")
      .then(setAllPeople)
      .catch(() => undefined);
  }, []);

  const save = async (draft: PersonDraft) => {
    if (!person) return;
    setBusy(true);
    try {
      const payload = personPayload(draft);
      await peopleRequest(`/${person.person_id}`, {
        method: "PATCH",
        body: JSON.stringify({
          ...payload,
          clear_date_of_birth: !payload.date_of_birth,
          clear_profile_asset: !payload.profile_asset_id,
        }),
      });
      if (payload.relationship_label)
        await peopleRequest(`/${person.person_id}/relationship`, {
          method: "PUT",
          body: JSON.stringify({ relationship_label: payload.relationship_label }),
        });
      else if (person.relationship_label)
        await peopleRequest(`/${person.person_id}/relationship`, { method: "DELETE" });
      setEditing(false);
      await load();
    } catch (requestError) {
      setError(
        (requestError as Error & { status?: number }).status === 409
          ? "Mark a Person as Me before assigning a relationship."
          : "Changes could not be saved. Please try again.",
      );
    } finally {
      setBusy(false);
    }
  };
  const setMe = async () => {
    if (!person) return;
    setBusy(true);
    try {
      await peopleRequest("/me", {
        method: "PUT",
        body: JSON.stringify({ person_id: person.person_id }),
      });
      await load();
    } catch {
      setError("Me could not be updated.");
    } finally {
      setBusy(false);
    }
  };
  const clearMe = async () => {
    setBusy(true);
    try {
      await peopleRequest("/me", { method: "DELETE" });
      await load();
    } catch {
      setError("Me could not be cleared.");
    } finally {
      setBusy(false);
    }
  };
  const merge = async () => {
    if (!person || !mergeSourceId) return;
    setBusy(true);
    try {
      await peopleRequest(`/${person.person_id}/merge`, {
        method: "POST",
        body: JSON.stringify({ source_person_id: mergeSourceId }),
      });
      setMergeOpen(false);
      setMergeSourceId("");
      await load();
    } catch (requestError) {
      setError(
        (requestError as Error & { status?: number }).status === 409
          ? "These People have conflicting corrections or relationships. Resolve those first."
          : "The duplicate could not be merged.",
      );
    } finally {
      setBusy(false);
    }
  };
  const correctAssociation = async () => {
    if (!person || !correctionAssetId) return;
    setBusy(true);
    try {
      let targetId = correctionPersonId;
      if (!targetId && newPersonName.trim()) {
        const created = await peopleRequest<Person>("", {
          method: "POST",
          body: JSON.stringify({ full_name: newPersonName.trim() }),
        });
        targetId = created.person_id;
      }
      if (!targetId) throw new Error("Choose or create a Person");
      await peopleRequest(`/${person.person_id}/assets`, {
        method: "PUT",
        body: JSON.stringify({
          asset_id: correctionAssetId,
          person_id: targetId,
          decision: "include",
        }),
      });
      setCorrectionOpen(false);
      setCorrectionAssetId("");
      setCorrectionPersonId("");
      setNewPersonName("");
      await load();
    } catch {
      setError("The Gallery association could not be corrected.");
    } finally {
      setBusy(false);
    }
  };
  const profile = profileAssets.find((asset) => asset.asset_id === person?.profile_asset_id);

  if (error)
    return (
      <div className="mx-auto max-w-5xl space-y-5">
        <Link
          className="pv-btn-ghost inline-flex items-center gap-2 px-3 py-2 text-sm"
          to="/app/people"
        >
          <ArrowLeft size={16} /> People
        </Link>
        <div className="pv-panel pv-status-error p-10 text-center text-sm">{error}</div>
      </div>
    );
  if (!person)
    return (
      <div className="mx-auto max-w-5xl">
        <div className="pv-panel h-80 animate-pulse" />
      </div>
    );
  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <Link
        className="pv-btn-ghost inline-flex items-center gap-2 px-3 py-2 text-sm"
        to="/app/people"
      >
        <ArrowLeft size={16} /> People
      </Link>
      <section className="pv-panel overflow-hidden">
        <div className="grid md:grid-cols-[minmax(220px,320px)_1fr]">
          <div
            className="relative aspect-square overflow-hidden"
            style={{ background: "linear-gradient(145deg, #23232a, #101014)" }}
          >
            {profile ? (
              <img
                className="absolute max-w-none object-cover"
                src={profile.thumbnail_url}
                alt={`${person.full_name} profile`}
                style={profileImageCropStyle(person.profile_frame)}
              />
            ) : (
              <div className="flex h-full items-center justify-center">
                <UserRound size={64} style={{ color: "var(--pv-silver-dim)" }} />
              </div>
            )}
          </div>
          <div className="flex flex-col justify-between gap-6 p-6 md:p-8">
            <div>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h2 className="pv-content-title text-2xl md:text-3xl">{person.full_name}</h2>
                  {person.preferred_name && (
                    <p className="mt-2 text-sm" style={{ color: "var(--pv-text-dim)" }}>
                      {person.preferred_name}
                    </p>
                  )}
                </div>
                {person.is_me && (
                  <span
                    className="inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs"
                    style={{ color: "var(--pv-gold-bright)", borderColor: "var(--pv-gold)" }}
                  >
                    <Star size={13} /> Me
                  </span>
                )}
              </div>
              <dl className="mt-6 grid gap-4 text-sm sm:grid-cols-2">
                <Info label="Date of birth" value={person.date_of_birth ?? "Not recorded"} />
                <Info label="Relationship" value={person.relationship_label ?? "Not recorded"} />
                {person.aliases.length > 0 && (
                  <Info label="Also known as" value={person.aliases.join(", ")} />
                )}
              </dl>
            </div>
            <div className="flex flex-wrap gap-3">
              <button
                className="pv-btn-primary inline-flex items-center gap-2 px-4 py-2 text-sm"
                onClick={() => setEditing(true)}
              >
                <Pencil size={16} /> Edit Person
              </button>
              <button
                className="pv-btn-secondary px-4 py-2 text-sm"
                disabled={busy}
                onClick={() => setCorrectionOpen(true)}
              >
                Correct association
              </button>
              <button
                className="pv-btn-ghost px-4 py-2 text-sm"
                disabled={busy}
                onClick={() => setMergeOpen(true)}
              >
                Merge duplicate
              </button>
              {person.is_me ? (
                <button
                  className="pv-btn-secondary px-4 py-2 text-sm"
                  disabled={busy}
                  onClick={() => void clearMe()}
                >
                  Clear Me
                </button>
              ) : (
                <button
                  className="pv-btn-secondary px-4 py-2 text-sm"
                  disabled={busy}
                  onClick={() => void setMe()}
                >
                  Mark as Me
                </button>
              )}
            </div>
          </div>
        </div>
      </section>
      <section className="space-y-3">
        <div>
          <h3 className="pv-content-title text-xl">Gallery assets</h3>
          <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
            {person.associated_asset_count
              ? `${person.associated_asset_count} associated Gallery ${person.associated_asset_count === 1 ? "asset" : "assets"}`
              : "No associated Gallery assets yet."}
          </p>
        </div>
        {person.associated_assets.length > 0 && (
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
            {person.associated_assets.map((asset) => {
              const gallery = profileAssets.find((image) => image.asset_id === asset.asset_id);
              return gallery ? (
                <Link
                  key={asset.asset_id}
                  to="/app/gallery/$photoId"
                  params={{ photoId: gallery.id }}
                  className="pv-panel pv-panel-hover overflow-hidden"
                >
                  <div className="aspect-square">
                    <img
                      className="h-full w-full object-cover"
                      src={gallery.thumbnail_url}
                      alt={asset.display_title}
                    />
                  </div>
                  <p className="truncate p-3 text-xs" style={{ color: "var(--pv-silver)" }}>
                    {asset.display_title}
                  </p>
                </Link>
              ) : (
                <div
                  key={asset.asset_id}
                  className="pv-panel flex aspect-square flex-col items-center justify-center p-3 text-center"
                >
                  <ImageIcon size={22} style={{ color: "var(--pv-silver-dim)" }} />
                  <p className="mt-2 line-clamp-2 text-xs" style={{ color: "var(--pv-silver)" }}>
                    {asset.display_title}
                  </p>
                </div>
              );
            })}
          </div>
        )}
      </section>
      <Dialog open={editing} onOpenChange={setEditing}>
        <DialogContent
          className="max-h-[90vh] max-w-2xl overflow-y-auto"
          style={{ background: "var(--pv-panel-2)", borderColor: "var(--pv-border-strong)" }}
        >
          <DialogHeader>
            <DialogTitle style={{ color: "var(--pv-gold-bright)" }}>Edit Person</DialogTitle>
            <DialogDescription>
              Person identity remains stable while these details change.
            </DialogDescription>
          </DialogHeader>
          <PersonForm
            initial={personDraftFrom(person)}
            submitLabel="Save changes"
            busy={busy}
            onSubmit={save}
            onCancel={() => setEditing(false)}
          />
        </DialogContent>
      </Dialog>
      <Dialog open={mergeOpen} onOpenChange={setMergeOpen}>
        <DialogContent
          className="max-w-lg"
          style={{ background: "var(--pv-panel-2)", borderColor: "var(--pv-border-strong)" }}
        >
          <DialogHeader>
            <DialogTitle style={{ color: "var(--pv-gold-bright)" }}>
              Merge duplicate into {person.full_name}
            </DialogTitle>
            <DialogDescription>
              The duplicate becomes inactive; its Gallery links, face references, Me mapping,
              relationships, corrections, and provenance move safely to this Person.
            </DialogDescription>
          </DialogHeader>
          <select
            className="pv-input w-full"
            value={mergeSourceId}
            onChange={(event) => setMergeSourceId(event.target.value)}
          >
            <option value="">Choose duplicate…</option>
            {allPeople
              .filter((candidate) => candidate.person_id !== person.person_id)
              .map((candidate) => (
                <option key={candidate.person_id} value={candidate.person_id}>
                  {candidate.full_name}
                </option>
              ))}
          </select>
          <div className="flex justify-end gap-3">
            <button className="pv-btn-ghost px-4 py-2 text-sm" onClick={() => setMergeOpen(false)}>
              Cancel
            </button>
            <button
              className="pv-btn-primary px-4 py-2 text-sm"
              disabled={!mergeSourceId || busy}
              onClick={() => void merge()}
            >
              {busy ? "Merging…" : "Merge safely"}
            </button>
          </div>
        </DialogContent>
      </Dialog>
      <Dialog open={correctionOpen} onOpenChange={setCorrectionOpen}>
        <DialogContent
          className="max-w-lg"
          style={{ background: "var(--pv-panel-2)", borderColor: "var(--pv-border-strong)" }}
        >
          <DialogHeader>
            <DialogTitle style={{ color: "var(--pv-gold-bright)" }}>
              Correct Gallery association
            </DialogTitle>
            <DialogDescription>
              Move one Gallery asset from this Person to an existing or newly created Person. Your
              correction remains authoritative.
            </DialogDescription>
          </DialogHeader>
          <select
            className="pv-input w-full"
            value={correctionAssetId}
            onChange={(event) => setCorrectionAssetId(event.target.value)}
          >
            <option value="">Choose Gallery asset…</option>
            {person.associated_assets.map((asset) => (
              <option key={asset.asset_id} value={asset.asset_id}>
                {asset.display_title}
              </option>
            ))}
          </select>
          <select
            className="pv-input w-full"
            value={correctionPersonId}
            onChange={(event) => {
              setCorrectionPersonId(event.target.value);
              setNewPersonName("");
            }}
          >
            <option value="">Create a new Person instead…</option>
            {allPeople
              .filter((candidate) => candidate.person_id !== person.person_id)
              .map((candidate) => (
                <option key={candidate.person_id} value={candidate.person_id}>
                  {candidate.full_name}
                </option>
              ))}
          </select>
          {!correctionPersonId && (
            <input
              className="pv-input w-full"
              value={newPersonName}
              onChange={(event) => setNewPersonName(event.target.value)}
              placeholder="New Person’s full name"
            />
          )}
          <div className="flex justify-end gap-3">
            <button
              className="pv-btn-ghost px-4 py-2 text-sm"
              onClick={() => setCorrectionOpen(false)}
            >
              Cancel
            </button>
            <button
              className="pv-btn-primary px-4 py-2 text-sm"
              disabled={
                !correctionAssetId || (!correctionPersonId && !newPersonName.trim()) || busy
              }
              onClick={() => void correctAssociation()}
            >
              {busy ? "Saving…" : "Save correction"}
            </button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide" style={{ color: "var(--pv-text-dim)" }}>
        {label}
      </dt>
      <dd className="mt-1" style={{ color: "var(--pv-silver)" }}>
        {value}
      </dd>
    </div>
  );
}
