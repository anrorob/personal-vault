import { useEffect, useRef, useState } from "react";

type Tag = { id: string; display_name: string };
async function request(path: string, method = "GET", body?: object) {
  const response = await fetch(path, {
    method,
    credentials: "include",
    ...(body
      ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : {}),
  });
  if (!response.ok) throw new Error("The change could not be saved. Please try again.");
  return response.status === 204 ? null : response.json();
}

export function HomeVideoPrivateTags({
  assetId,
  onChange,
}: {
  assetId: string;
  onChange: () => void;
}) {
  const [tags, setTags] = useState<Tag[]>([]);
  const [choices, setChoices] = useState<Tag[]>([]);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const path = `/api/personal-videos/assets/${assetId}/private-tags`;
  useEffect(() => {
    let current = true;
    void Promise.all([request(path), request("/api/gallery/custom-tags")])
      .then(([assigned, all]) => {
        if (current) {
          setTags(assigned);
          setChoices(all);
        }
      })
      .catch(() => {
        if (current) setError("Private tags could not be loaded.");
      });
    return () => {
      current = false;
    };
  }, [path]);
  async function change(method: string, tagId?: string) {
    setBusy(true);
    setError("");
    try {
      await request(
        tagId ? `${path}/${tagId}` : path,
        method,
        method === "POST" ? { display_name: name } : undefined,
      );
      const [assigned, all] = await Promise.all([
        request(path),
        request("/api/gallery/custom-tags"),
      ]);
      setTags(assigned);
      setChoices(all);
      setName("");
      onChange();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="space-y-2 min-w-0 pt-3 border-t border-[var(--pv-border)]"
      aria-label="My private tags"
    >
      <h3 className="font-medium text-sm">My private tags</h3>
      <p className="text-xs">Visible only to you.</p>
      <div className="flex flex-wrap gap-2">
        {tags.map((tag) => (
          <button
            type="button"
            className="pv-btn-secondary text-xs break-all"
            disabled={busy}
            key={tag.id}
            aria-label={`Remove private tag ${tag.display_name}`}
            onClick={() => void change("DELETE", tag.id)}
          >
            {tag.display_name} ×
          </button>
        ))}
      </div>
      <select
        className="pv-input w-full min-w-0"
        aria-label="Attach private tag"
        value=""
        disabled={busy}
        onChange={(e) => {
          if (e.target.value) void change("PUT", e.target.value);
        }}
      >
        <option value="">Attach private tag…</option>
        {choices
          .filter((tag) => !tags.some((value) => value.id === tag.id))
          .map((tag) => (
            <option key={tag.id} value={tag.id}>
              {tag.display_name}
            </option>
          ))}
      </select>
      <form
        className="flex flex-wrap gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          void change("POST");
        }}
      >
        <input
          aria-label="New private tag"
          className="pv-input min-w-0 flex-1"
          maxLength={64}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <button className="pv-btn-secondary" type="submit" disabled={busy || !name.trim()}>
          Create private tag
        </button>
      </form>
      {error && (
        <p role="alert" className="text-red-300 text-xs">
          {error}
        </p>
      )}
    </section>
  );
}

export function HomeVideoCaptureDate({
  assetId,
  capturedOn,
  onSaved,
}: {
  assetId: string;
  capturedOn: string | null;
  onSaved: (value: string) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(capturedOn ?? "");
  const [savedDate, setSavedDate] = useState(capturedOn);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const trigger = useRef<HTMLButtonElement>(null);
  const dateInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (editing) dateInput.current?.focus({ preventScroll: true });
  }, [editing]);
  useEffect(() => setSavedDate(capturedOn), [capturedOn]);
  function close() {
    setEditing(false);
    trigger.current?.focus({ preventScroll: true });
  }
  async function save() {
    setBusy(true);
    setError("");
    try {
      const result = await request(`/api/vault-master/assets/${assetId}/metadata`, "PATCH", {
        captured_on: draft,
      });
      setSavedDate(result.captured_on);
      close();
      await onSaved(result.captured_on);
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="space-y-2 min-w-0 pt-3 border-t border-[var(--pv-border)]"
      aria-label="Capture date"
    >
      <h3 className="font-medium text-sm">Capture date</h3>
      <p>{savedDate ?? "Date not recorded"}</p>
      <button
        ref={trigger}
        type="button"
        className="pv-btn-secondary"
        aria-expanded={editing}
        onClick={() => {
          setDraft(savedDate ?? "");
          setError("");
          setEditing(true);
        }}
      >
        {savedDate ? "Edit date" : "Add date"}
      </button>
      {editing && (
        <form
          data-home-video-date-editor
          className="space-y-2"
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.stopPropagation();
              if (!busy) close();
            }
          }}
        >
          <label className="block">
            Recording date
            <input
              ref={dateInput}
              type="date"
              className="pv-input w-full min-w-0 max-w-full"
              value={draft}
              required
              onChange={(e) => setDraft(e.target.value)}
            />
          </label>
          <div className="flex flex-wrap gap-2">
            <button type="submit" className="pv-btn-secondary" disabled={busy || !draft}>
              Save date
            </button>
            <button type="button" className="pv-btn-secondary" disabled={busy} onClick={close}>
              Cancel
            </button>
          </div>
        </form>
      )}
      {error && (
        <p role="alert" className="text-red-300 text-xs">
          {error}
        </p>
      )}
    </section>
  );
}
