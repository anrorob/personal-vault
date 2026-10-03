import { Link } from "@tanstack/react-router";
import { Film } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { PlaybackStateIndicator } from "@/components/pv/PlaybackState";
import { useTheatreProgress } from "@/lib/theatre-progress";

import { franchiseRequest, type FranchiseMovie, type MovieFranchise } from "@/lib/movie-franchises";

const inputClass = "w-full rounded border border-[var(--pv-border)] bg-transparent p-2 text-sm";

export function FranchiseEditor({
  franchise,
  onSaved,
  open: controlledOpen,
  onOpenChange,
  showTrigger = true,
}: {
  franchise?: MovieFranchise;
  onSaved: (franchise: MovieFranchise) => void;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  showTrigger?: boolean;
}) {
  const [internalOpen, setInternalOpen] = useState(false);
  const open = controlledOpen ?? internalOpen;
  const setOpen = onOpenChange ?? setInternalOpen;
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (open) {
      setName(franchise?.name ?? "");
      setDescription(franchise?.description ?? "");
      setError(null);
    }
  }, [open, franchise]);
  return (
    <>
      {showTrigger && (
        <button type="button" className="pv-btn-ghost" onClick={() => setOpen(true)}>
          {franchise ? "Edit franchise" : "Create franchise"}
        </button>
      )}
      <Dialog
        open={open}
        onOpenChange={(value) => {
          if (!pending) setOpen(value);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{franchise ? "Edit franchise" : "Create franchise"}</DialogTitle>
            <DialogDescription>
              Choose the movies yourself. Release and available Timeline ordering are automatic.
            </DialogDescription>
          </DialogHeader>
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              setPending(true);
              setError(null);
              void franchiseRequest<MovieFranchise>(
                franchise ? `/${franchise.id}` : "",
                franchise ? "PATCH" : "POST",
                {
                  name: name.trim(),
                  description,
                },
              )
                .then((saved) => {
                  onSaved(saved);
                  setOpen(false);
                })
                .catch((reason: Error) => setError(reason.message))
                .finally(() => setPending(false));
            }}
          >
            <label className="block space-y-1 text-sm">
              Name
              <input
                className={inputClass}
                value={name}
                onChange={(event) => setName(event.target.value)}
                required
                maxLength={160}
                disabled={pending}
              />
            </label>
            <label className="block space-y-1 text-sm">
              Description (optional)
              <textarea
                className={inputClass}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                maxLength={4000}
                disabled={pending}
              />
            </label>
            {error && (
              <p role="alert" className="text-sm text-red-300">
                {error}
              </p>
            )}
            <button className="pv-btn-ghost" type="submit" disabled={pending || !name.trim()}>
              {pending ? "Saving…" : "Save franchise"}
            </button>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}

export function MovieFranchiseMembership({ movieId }: { movieId: string }) {
  const [franchises, setFranchises] = useState<MovieFranchise[] | null>(null);
  const [choice, setChoice] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    setFranchises(await franchiseRequest<MovieFranchise[]>());
  }, []);
  useEffect(() => {
    let active = true;
    void franchiseRequest<MovieFranchise[]>()
      .then((items) => {
        if (active) setFranchises(items);
      })
      .catch((reason: Error) => {
        if (active) setError(reason.message);
      });
    return () => {
      active = false;
    };
  }, [movieId]);
  const members = franchises?.filter((item) => item.member_ids.includes(movieId)) ?? [];
  const available = franchises?.filter((item) => !item.member_ids.includes(movieId)) ?? [];
  const change = async (id: string, remove = false) => {
    setPending(true);
    setError(null);
    try {
      await franchiseRequest(
        `/${id}/members${remove ? `/${movieId}` : ""}`,
        remove ? "DELETE" : "POST",
        remove ? undefined : { asset_ids: [movieId] },
      );
      await load();
      setChoice("");
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setPending(false);
    }
  };
  return (
    <section className="pv-panel p-5 space-y-3" aria-label="Movie franchises">
      <h2 className="text-lg font-semibold">Franchises</h2>
      {franchises === null && !error && <p className="text-sm">Loading franchises…</p>}
      {members.map((item) => (
        <div key={item.id} className="flex flex-wrap items-center gap-3">
          <Link
            to="/app/movies"
            search={{ franchise: item.id }}
            className="text-sm text-[var(--pv-gold)]"
          >
            {item.name}
          </Link>
          <button
            type="button"
            className="pv-btn-ghost"
            disabled={pending}
            onClick={() => void change(item.id, true)}
          >
            Remove from {item.name}
          </button>
        </div>
      ))}
      <div className="flex flex-wrap items-center gap-3">
        {available.length > 0 && (
          <>
            <select
              aria-label="Choose franchise"
              className="rounded border border-[var(--pv-border)] bg-[#111115] p-2 text-sm"
              value={choice}
              onChange={(event) => setChoice(event.target.value)}
              disabled={pending}
            >
              <option value="">Choose franchise</option>
              {available.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="pv-btn-ghost"
              disabled={pending || !choice}
              onClick={() => void change(choice)}
            >
              Add to franchise
            </button>
          </>
        )}
      </div>
      {error && (
        <p role="alert" className="text-sm text-red-300">
          {error}
        </p>
      )}
    </section>
  );
}

export function MovieFranchisePanel({ id, onDeleted }: { id: string; onDeleted: () => void }) {
  const [franchise, setFranchise] = useState<MovieFranchise | null>(null);
  const [library, setLibrary] = useState<FranchiseMovie[] | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [selection, setSelection] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { progress, error: progressError } = useTheatreProgress("movies");
  useEffect(() => {
    let active = true;
    setFranchise(null);
    setError(null);
    void franchiseRequest<MovieFranchise>(`/${id}`)
      .then((item) => {
        if (active) setFranchise(item);
      })
      .catch((reason: Error) => {
        if (active) setError(reason.message);
      });
    return () => {
      active = false;
    };
  }, [id]);
  const update = async (path: string, method: string, body?: unknown) => {
    setPending(true);
    setError(null);
    try {
      setFranchise(await franchiseRequest<MovieFranchise>(`/${id}${path}`, method, body));
      setAddOpen(false);
      setSelection([]);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setPending(false);
    }
  };
  const openAdd = async () => {
    setPending(true);
    setError(null);
    try {
      const response = await fetch("/api/movies", {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("The movie library is currently unavailable.");
      setLibrary((await response.json()) as FranchiseMovie[]);
      setSelection([]);
      setQuery("");
      setAddOpen(true);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setPending(false);
    }
  };
  return (
    <div className="space-y-6">
      <Link to="/app/movies" search={{}} className="pv-btn-ghost inline-block">
        Back to Movies
      </Link>
      {error && (
        <p role="alert" className="text-sm text-red-300">
          {error}
        </p>
      )}
      {progressError && (
        <p role="alert" className="text-sm text-red-300">
          {progressError}
        </p>
      )}
      {!franchise && !error && <p>Loading franchise…</p>}
      {franchise && (
        <>
          <div className="space-y-3">
            <h3 className="pv-content-title text-xl">{franchise.name}</h3>
            {franchise.description && (
              <p className="text-sm text-[var(--pv-text-dim)]">{franchise.description}</p>
            )}
            <div className="flex flex-wrap items-center gap-3">
              <label className="flex items-center gap-2 text-sm">
                Order
                <select
                  aria-label="Franchise order"
                  value={franchise.effective_order}
                  disabled={pending}
                  className="rounded border border-[var(--pv-border)] bg-[#111115] p-2"
                  onChange={(event) => void update("/order", "PUT", { order: event.target.value })}
                >
                  <option value="timeline" disabled={!franchise.timeline_available}>
                    Timeline{!franchise.timeline_available ? " (unavailable)" : ""}
                  </option>
                  <option value="release">Release</option>
                </select>
              </label>
              <button
                type="button"
                className="pv-btn-ghost"
                disabled={pending}
                onClick={() => void openAdd()}
              >
                Add movies
              </button>
              <FranchiseEditor franchise={franchise} onSaved={setFranchise} />
              <button
                type="button"
                className="pv-btn-ghost"
                disabled={pending}
                onClick={() => void update("/chronology/refresh", "POST")}
              >
                Refresh Timeline
              </button>
              <button
                type="button"
                className="pv-btn-ghost"
                disabled={pending}
                onClick={() => setDeleteOpen(true)}
              >
                Delete franchise
              </button>
            </div>
            {!franchise.timeline_available && (
              <p className="text-sm text-[var(--pv-text-dim)]">
                Timeline is unavailable for this collection. Showing Release order.
              </p>
            )}
            {franchise.timeline_available && franchise.chronology_url && (
              <p className="text-xs text-[var(--pv-text-dim)]">
                Timeline:{" "}
                <a
                  href={franchise.chronology_url}
                  target="_blank"
                  rel="noreferrer"
                  className="underline"
                >
                  {franchise.chronology_source}
                </a>
                . Stored locally.
              </p>
            )}
          </div>
          {franchise.movies.length === 0 && (
            <p className="pv-panel p-8 text-center text-sm">Add movies to this franchise.</p>
          )}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            {franchise.movies.map((movie) => (
              <div key={movie.id} className="pv-panel overflow-hidden">
                <Link
                  to="/app/movies/$movieId"
                  params={{ movieId: movie.id }}
                  className="block pv-panel-hover"
                  aria-label={`View details for ${movie.title}`}
                >
                  <div className="aspect-[2/3] relative flex items-center justify-center bg-[#111115]">
                    {movie.poster_url ? (
                      <img
                        src={movie.poster_url}
                        alt={`${movie.title} poster`}
                        loading="lazy"
                        className="absolute inset-0 h-full w-full object-cover"
                      />
                    ) : (
                      <Film size={40} className="text-[var(--pv-silver-dim)]" />
                    )}
                  </div>
                  <div className="p-4 space-y-1">
                    <h4 className="text-sm font-semibold">{movie.title}</h4>
                    <PlaybackStateIndicator progress={progress[movie.id]} />
                    {movie.year && (
                      <p className="text-xs text-[var(--pv-text-dim)]">{movie.year}</p>
                    )}
                  </div>
                </Link>
                <button
                  type="button"
                  className="pv-btn-ghost m-2"
                  disabled={pending}
                  onClick={() => void update(`/members/${movie.id}`, "DELETE")}
                >
                  Remove from franchise
                </button>
              </div>
            ))}
          </div>
          <Dialog
            open={addOpen}
            onOpenChange={(value) => {
              if (!pending) setAddOpen(value);
            }}
          >
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Add movies to {franchise.name}</DialogTitle>
                <DialogDescription>Select the movies you want in this franchise.</DialogDescription>
              </DialogHeader>
              <input
                aria-label="Filter movies to add"
                className={inputClass}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Title or year"
              />
              <div className="max-h-80 overflow-y-auto space-y-2">
                {library
                  ?.filter(
                    (movie) =>
                      !franchise.member_ids.includes(movie.id) &&
                      `${movie.title} ${movie.year ?? ""}`
                        .toLowerCase()
                        .includes(query.toLowerCase()),
                  )
                  .map((movie) => (
                    <label key={movie.id} className="flex gap-3 items-center text-sm p-2">
                      <input
                        type="checkbox"
                        checked={selection.includes(movie.id)}
                        disabled={pending}
                        onChange={(event) =>
                          setSelection((current) =>
                            event.target.checked
                              ? [...current, movie.id]
                              : current.filter((value) => value !== movie.id),
                          )
                        }
                      />
                      {movie.title}
                      {movie.year ? ` (${movie.year})` : ""}
                    </label>
                  ))}
              </div>
              {error && (
                <p role="alert" className="text-sm text-red-300">
                  {error}
                </p>
              )}
              <button
                type="button"
                className="pv-btn-ghost"
                disabled={pending || !selection.length}
                onClick={() => void update("/members", "POST", { asset_ids: selection })}
              >
                {pending ? "Adding…" : `Add selected movies (${selection.length})`}
              </button>
            </DialogContent>
          </Dialog>
          <Dialog
            open={deleteOpen}
            onOpenChange={(value) => {
              if (!pending) setDeleteOpen(value);
            }}
          >
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Delete {franchise.name}?</DialogTitle>
                <DialogDescription>
                  The franchise will be removed. All movies and their files stay in Movies.
                </DialogDescription>
              </DialogHeader>
              {error && (
                <p role="alert" className="text-sm text-red-300">
                  {error}
                </p>
              )}
              <button
                type="button"
                className="pv-btn-ghost"
                disabled={pending}
                onClick={() => {
                  setPending(true);
                  setError(null);
                  void franchiseRequest(`/${id}`, "DELETE")
                    .then(onDeleted)
                    .catch((reason: Error) => setError(reason.message))
                    .finally(() => setPending(false));
                }}
              >
                Delete franchise
              </button>
              <button
                type="button"
                className="pv-btn-ghost"
                disabled={pending}
                onClick={() => setDeleteOpen(false)}
              >
                Cancel
              </button>
            </DialogContent>
          </Dialog>
        </>
      )}
    </div>
  );
}
