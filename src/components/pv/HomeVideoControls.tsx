import { useEffect, useRef, useState } from "react";
import { GalleryControls } from "./GalleryControls";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";

import { initialHomeVideoQuery, type HomeVideoQuery } from "@/lib/home-video-controls";

type Choice = { id: string; display_name: string };
type Progress = {
  pending: number;
  queued: number;
  processing: number;
  completed: number;
  failed: number;
  skipped: number;
  already_current?: number;
  queued_new?: number;
  skipped_active?: number;
  failed_existing_requires_attention?: number;
};
async function request(url: string, options?: RequestInit) {
  const response = await fetch(url, { credentials: "include", ...options });
  const body = await response.json().catch(() => null);
  if (!response.ok)
    throw new Error(
      typeof body?.detail === "string"
        ? body.detail
        : "The request could not be completed. Please try again.",
    );
  return body;
}

export function HomeVideoControls({
  query,
  onQuery,
  selecting,
  selectedIds,
  onSelect,
  onClear,
  onHidden,
  onHideComplete,
  onError,
}: {
  query: HomeVideoQuery;
  onQuery: (value: HomeVideoQuery) => void;
  selecting: boolean;
  selectedIds: string[];
  onSelect: (value: boolean) => void;
  onClear: () => void;
  onHidden: () => Promise<void>;
  onHideComplete: (ids: string[], all: boolean) => void;
  onError: (value: string | null) => void;
}) {
  const dialogTrigger = useRef<HTMLElement | null>(null);
  const rememberTrigger = () => {
    dialogTrigger.current = document.activeElement as HTMLElement;
  };
  const [dialog, setDialog] = useState<"filter" | "shared" | "share" | null>(null);
  const [choices, setChoices] = useState<{
    people: Choice[];
    private_tags: Choice[];
    content_tags?: { slug: string; display_name: string }[];
    locations: string[];
  } | null>(null);
  const [draft, setDraft] = useState(query);
  const [busy, setBusy] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [recipients, setRecipients] = useState<{ user_id: string; display_name: string }[]>([]);
  const [recipientIds, setRecipientIds] = useState<string[]>([]);
  const [shareTarget, setShareTarget] = useState("specific");
  const [shareMode, setShareMode] = useState("quick");
  const [progress, setProgress] = useState<Progress | null>(null);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = (await request("/api/personal-videos/ken/backfill")) as Progress;
        if (!cancelled) setProgress(value);
      } catch {
        /* The action reports availability; playback remains independent. */
      }
      if (!cancelled) timer = setTimeout(() => void poll(), 5000);
    };
    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, []);
  useEffect(() => {
    if (dialog !== "filter") return;
    const controller = new AbortController();
    setChoices(null);
    setDialogError(null);
    void request(
      `/api/personal-videos/filter-options?include_hidden=${query.include_hidden}&include_shared=${query.include_shared}`,
      { signal: controller.signal },
    )
      .then((body) => {
        if (!controller.signal.aborted) setChoices(body);
      })
      .catch(() => {
        if (!controller.signal.aborted) setDialogError("Filter choices could not be loaded.");
      });
    return () => controller.abort();
  }, [dialog, query.include_hidden, query.include_shared]);
  async function hideSelected() {
    setBusy(true);
    onError(null);
    const results = await Promise.allSettled(
      selectedIds.map(async (id) => {
        await request(
          `/api/vault-master/assets/${id}/lifecycle/${query.include_hidden ? "unhide" : "hide"}`,
          { method: "POST" },
        );
        return id;
      }),
    );
    const successes = results.flatMap((result) =>
      result.status === "fulfilled" ? [result.value] : [],
    );
    const failure = results.find((result) => result.status === "rejected");
    onHideComplete(successes, !failure);
    if (failure?.status === "rejected")
      onError(
        failure.reason instanceof Error
          ? failure.reason.message
          : "Some videos could not be updated.",
      );
    setBusy(false);
  }
  async function openShare() {
    rememberTrigger();
    setDialog("share");
    setDialogError(null);
    setRecipients([]);
    setRecipientIds([]);
    setShareMode(selectedIds.length > 10 ? "standard" : "quick");
    try {
      const body = await request(`/api/vault-master/assets/${selectedIds[0]}/sharing`);
      setRecipients(body.eligible_users);
    } catch (error) {
      setDialogError((error as Error).message);
    }
  }
  async function share() {
    if (shareTarget === "specific" && !recipientIds.length) {
      setDialogError("Select at least one person.");
      return;
    }
    setBusy(true);
    setDialogError(null);
    try {
      await request("/api/vault-master/assets/sharing/bulk", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          asset_ids: selectedIds,
          mode: shareTarget,
          recipient_user_ids: shareTarget === "specific" ? recipientIds : [],
          share_mode: shareMode,
        }),
      });
      setDialog(null);
      onClear();
      onSelect(false);
    } catch (error) {
      setDialogError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function analyse() {
    setBusy(true);
    onError(null);
    try {
      setProgress(await request("/api/personal-videos/ken/backfill", { method: "POST" }));
    } catch (error) {
      onError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const activeCount = progress ? progress.pending + progress.queued + progress.processing : 0;
  return (
    <>
      <GalleryControls
        sectionName="Home Videos"
        actions={
          <>
            <button
              className="pv-btn-secondary"
              onClick={() => {
                rememberTrigger();
                setDraft(query);
                setDialog("filter");
              }}
            >
              Filter
            </button>
            <button
              className="pv-btn-secondary"
              onClick={() => {
                rememberTrigger();
                setDialogError(null);
                setDialog("shared");
              }}
            >
              Shared videos
            </button>
            <button className="pv-btn-secondary" onClick={() => onSelect(true)}>
              Select
            </button>
            <button className="pv-btn-secondary" disabled={busy} onClick={() => void onHidden()}>
              {query.include_hidden ? "Leave Hidden Videos" : "View Hidden"}
            </button>
            <button className="pv-btn-secondary" disabled={busy} onClick={() => void analyse()}>
              Analyse existing videos
            </button>
          </>
        }
        sort={
          <label className="flex items-center gap-2 text-sm">
            Sort
            <select
              className="pv-input max-w-40"
              value={query.sort}
              onChange={(e) =>
                onQuery({ ...query, sort: e.target.value as HomeVideoQuery["sort"] })
              }
            >
              <option value="newest">Newest first</option>
              <option value="oldest">Oldest first</option>
              <option value="name_asc">Title A–Z</option>
              <option value="name_desc">Title Z–A</option>
            </select>
          </label>
        }
        selection={
          selecting ? (
            <div
              className="flex min-w-0 flex-wrap items-center gap-2"
              aria-label="Selected video actions"
            >
              <span role="status" className="text-sm">
                {selectedIds.length} selected
              </span>
              {!query.include_hidden && (
                <button
                  className="pv-btn-secondary"
                  disabled={busy || !selectedIds.length}
                  onClick={() => void openShare()}
                >
                  Share
                </button>
              )}
              <button
                className="pv-btn-secondary"
                disabled={busy || !selectedIds.length}
                onClick={() => void hideSelected()}
              >
                {query.include_hidden ? "Restore selected" : "Hide selected"}
              </button>
              <button className="pv-btn-secondary" disabled={busy} onClick={onClear}>
                Clear
              </button>
              <button
                className="pv-btn-secondary"
                disabled={busy}
                onClick={() => {
                  onClear();
                  onSelect(false);
                }}
              >
                Done
              </button>
            </div>
          ) : undefined
        }
      />
      {query.include_hidden && (
        <p className="text-sm">Hidden Videos · visible only in this unlocked session</p>
      )}
      {progress &&
        (activeCount > 0 ||
          progress.completed > 0 ||
          progress.failed > 0 ||
          progress.already_current !== undefined) && (
          <p role="status" className="text-sm">
            KEN: {progress.pending + progress.queued} pending · {progress.processing} processing ·{" "}
            {progress.completed} completed · {progress.failed} failed
            {progress.queued_new !== undefined ? ` · ${progress.queued_new} newly queued` : ""}
            {progress.skipped_active !== undefined
              ? ` · ${progress.skipped_active} already active`
              : ""}
            {progress.already_current !== undefined
              ? ` · ${progress.already_current} already current`
              : ""}
            {progress.failed > 0
              ? " · Open the video's KEN details for the failure reason and retry availability."
              : ""}
          </p>
        )}
      <Dialog
        open={dialog !== null}
        onOpenChange={(open) => {
          if (!open && !busy) setDialog(null);
        }}
      >
        <DialogContent
          className="pv-dialog max-h-[85dvh] overflow-y-auto sm:max-w-lg"
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            const trigger = dialogTrigger.current;
            const menu = trigger?.closest('[data-context-open="false"]');
            const mobile = window.matchMedia("(max-width: 767px)").matches;
            const target =
              menu && mobile
                ? document.querySelector<HTMLElement>('button[aria-label="Home Videos actions"]')
                : trigger;
            target?.focus({ preventScroll: true });
          }}
        >
          <DialogTitle>
            {dialog === "filter"
              ? "Filter Home Videos"
              : dialog === "shared"
                ? "Shared videos"
                : "Share selected videos"}
          </DialogTitle>
          <DialogDescription>
            {dialog === "filter"
              ? "Filter by your video metadata."
              : dialog === "shared"
                ? "Choose whether authorized shared videos appear in Home Videos."
                : "Share in your Vault using the existing sharing permissions."}
          </DialogDescription>
          {dialogError && (
            <p role="alert" className="text-sm text-red-300">
              {dialogError}
            </p>
          )}
          {dialog === "filter" && (
            <>
              {!choices && !dialogError && <p>Loading filters…</p>}
              {choices && (
                <>
                  <label>
                    People
                    <select
                      className="pv-input w-full"
                      value={draft.person}
                      onChange={(e) => setDraft({ ...draft, person: e.target.value })}
                    >
                      <option value="">All people</option>
                      {choices.people.map((person) => (
                        <option key={person.id} value={person.id}>
                          {person.display_name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    System content tags
                    <select
                      className="pv-input w-full min-w-0"
                      value={draft.content_tag}
                      onChange={(e) => setDraft({ ...draft, content_tag: e.target.value })}
                    >
                      <option value="">All system content tags</option>
                      {(choices.content_tags ?? []).map((tag) => (
                        <option key={tag.slug} value={tag.slug}>
                          {tag.display_name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    My private tags
                    <select
                      className="pv-input w-full"
                      value={draft.private_tag}
                      onChange={(e) => setDraft({ ...draft, private_tag: e.target.value })}
                    >
                      <option value="">All private tags</option>
                      {choices.private_tags.map((tag) => (
                        <option key={tag.id} value={tag.id}>
                          {tag.display_name}
                        </option>
                      ))}
                    </select>
                    <span className="block text-xs">Visible only to you.</span>
                  </label>
                  <label>
                    Recorded from
                    <input
                      className="pv-input w-full min-w-0"
                      type="date"
                      value={draft.date_from}
                      onChange={(e) => setDraft({ ...draft, date_from: e.target.value })}
                    />
                  </label>
                  <label>
                    Recorded to
                    <input
                      className="pv-input w-full min-w-0"
                      type="date"
                      value={draft.date_to}
                      onChange={(e) => setDraft({ ...draft, date_to: e.target.value })}
                    />
                  </label>
                  <label>
                    Location
                    <input
                      className="pv-input w-full"
                      list="home-video-locations"
                      value={draft.location}
                      onChange={(e) => setDraft({ ...draft, location: e.target.value })}
                    />
                  </label>
                  <datalist id="home-video-locations">
                    {choices.locations.map((value) => (
                      <option key={value} value={value} />
                    ))}
                  </datalist>
                  <div className="flex flex-wrap gap-2">
                    <button
                      className="pv-btn-primary"
                      onClick={() => {
                        onQuery(draft);
                        setDialog(null);
                      }}
                    >
                      Apply filters
                    </button>
                    <button
                      className="pv-btn-secondary"
                      onClick={() => {
                        const cleared = {
                          ...initialHomeVideoQuery,
                          sort: query.sort,
                          include_hidden: query.include_hidden,
                          include_shared: query.include_shared,
                        };
                        setDraft(cleared);
                        onQuery(cleared);
                      }}
                    >
                      Clear filters
                    </button>
                  </div>
                </>
              )}
            </>
          )}
          {dialog === "shared" && (
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={query.include_shared}
                disabled={query.include_hidden}
                onChange={(e) => onQuery({ ...query, include_shared: e.target.checked })}
              />
              Include shared videos
            </label>
          )}
          {dialog === "share" && (
            <>
              <label>
                Who can see these videos?
                <select
                  className="pv-input w-full"
                  value={shareTarget}
                  onChange={(e) => setShareTarget(e.target.value)}
                >
                  <option value="specific">Specific people</option>
                  <option value="everyone">Everyone in my Vault</option>
                </select>
              </label>
              {shareTarget === "specific" &&
                recipients.map((person) => (
                  <label key={person.user_id} className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={recipientIds.includes(person.user_id)}
                      onChange={(e) =>
                        setRecipientIds((ids) =>
                          e.target.checked
                            ? [...ids, person.user_id]
                            : ids.filter((id) => id !== person.user_id),
                        )
                      }
                    />
                    {person.display_name}
                  </label>
                ))}
              <label>
                Sharing mode
                <select
                  className="pv-input w-full"
                  value={shareMode}
                  onChange={(e) => setShareMode(e.target.value)}
                >
                  <option value="quick">Quick share</option>
                  <option value="standard">Standard share (3 minute review)</option>
                </select>
              </label>
              <button className="pv-btn-primary" disabled={busy} onClick={() => void share()}>
                Save sharing
              </button>
            </>
          )}
          <button className="pv-btn-secondary" disabled={busy} onClick={() => setDialog(null)}>
            Close
          </button>
        </DialogContent>
      </Dialog>
    </>
  );
}
