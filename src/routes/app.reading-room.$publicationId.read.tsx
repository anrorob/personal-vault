import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import {
  Bookmark,
  BookmarkMinus,
  BookOpenText,
  Check,
  ChevronLeft,
  List,
  Minus,
  Plus,
} from "lucide-react";
import { createElement, useCallback, useEffect, useMemo, useRef, useState } from "react";

export const Route = createFileRoute("/app/reading-room/$publicationId/read")({
  validateSearch: (search: Record<string, unknown>) => ({
    locator:
      typeof search.locator === "string" && search.locator.length <= 240
        ? search.locator
        : undefined,
  }),
  component: PersonalVaultReader,
});

type ReaderBlock = {
  locator: string;
  parent_locator: string | null;
  block_type:
    | "part"
    | "chapter"
    | "heading"
    | "paragraph"
    | "footnote"
    | "illustration"
    | "caption"
    | "page_marker"
    | "table"
    | "other";
  text: string | null;
  illustration_url: string | null;
};

type ReaderBookmark = {
  id: string;
  locator: string;
  character_offset: number;
  label: string | null;
  created_at: string;
};

type ReaderDocument = {
  id: string;
  title: string;
  author: string;
  language: string | null;
  content_version: string | null;
  blocks: ReaderBlock[];
  chapters: Array<{ locator: string; title: string; level: number }>;
  position: {
    locator: string;
    character_offset: number;
    completed: boolean;
    percent: number;
  } | null;
  preferences: { theme?: unknown; font_family?: unknown; font_size?: unknown };
  bookmarks: ReaderBookmark[];
};

type Theme = "light" | "dark" | "sepia";
type FontFamily = "serif" | "sans";

const THEMES: Record<Theme, { background: string; page: string; text: string; muted: string }> = {
  light: { background: "#e8e8e6", page: "#ffffff", text: "#252525", muted: "#66645f" },
  dark: { background: "#080809", page: "#131316", text: "#ddd9cf", muted: "#9b978f" },
  sepia: { background: "#cfc2a3", page: "#f3ead5", text: "#382f25", muted: "#756958" },
};

const safeTheme = (value: unknown): Theme =>
  value === "light" || value === "dark" || value === "sepia" ? value : "sepia";
const safeFamily = (value: unknown): FontFamily => (value === "sans" ? "sans" : "serif");
const safeSize = (value: unknown) =>
  typeof value === "number" ? Math.min(32, Math.max(14, value)) : 18;

function PersonalVaultReader() {
  const { publicationId } = Route.useParams();
  const { locator: requestedLocator } = Route.useSearch();
  const navigate = useNavigate();
  const [document, setDocument] = useState<ReaderDocument | null>(null);
  const [theme, setTheme] = useState<Theme>("sepia");
  const [fontFamily, setFontFamily] = useState<FontFamily>("serif");
  const [fontSize, setFontSize] = useState(18);
  const [currentLocator, setCurrentLocator] = useState<string | null>(null);
  const [bookmarks, setBookmarks] = useState<ReaderBookmark[]>([]);
  const [navigationOpen, setNavigationOpen] = useState(false);
  const [completed, setCompleted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const restored = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    void fetch(`/api/reading-room/publications/${publicationId}/reader`, {
      credentials: "include",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (response.status === 401) {
          await navigate({ to: "/login" });
          return null;
        }
        if (!response.ok) throw new Error("Reader request failed");
        return (await response.json()) as ReaderDocument;
      })
      .then((reader) => {
        if (!reader) return;
        setDocument(reader);
        setTheme(safeTheme(reader.preferences.theme));
        setFontFamily(safeFamily(reader.preferences.font_family));
        setFontSize(safeSize(reader.preferences.font_size));
        const validRequestedLocator = reader.blocks.some(
          (block) => block.locator === requestedLocator,
        )
          ? requestedLocator
          : null;
        setCurrentLocator(
          validRequestedLocator ?? reader.position?.locator ?? reader.blocks[0]?.locator ?? null,
        );
        setCompleted(reader.position?.completed ?? false);
        setBookmarks(reader.bookmarks);
      })
      .catch((requestError) => {
        if (!(requestError instanceof DOMException && requestError.name === "AbortError")) {
          setError("This publication cannot be opened in the native reader.");
        }
      });
    return () => controller.abort();
  }, [navigate, publicationId, requestedLocator]);

  useEffect(() => {
    if (!document || !currentLocator || restored.current) return;
    restored.current = true;
    requestAnimationFrame(() => {
      documentLocator(currentLocator)?.scrollIntoView({ block: "start" });
    });
  }, [currentLocator, document]);

  useEffect(() => {
    if (!document) return;
    const elements = document.blocks
      .map((block) => documentLocator(block.locator))
      .filter((element): element is HTMLElement => Boolean(element));
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((first, second) => first.boundingClientRect.top - second.boundingClientRect.top)[0];
        if (visible) {
          setCurrentLocator((visible.target as HTMLElement).dataset.locator ?? null);
        }
      },
      { rootMargin: "-12% 0px -72% 0px", threshold: 0 },
    );
    elements.forEach((element) => observer.observe(element));
    return () => observer.disconnect();
  }, [document]);

  useEffect(() => {
    if (!document || !currentLocator) return;
    const timer = window.setTimeout(() => {
      void fetch(`/api/reading-room/publications/${publicationId}/position`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          locator: currentLocator,
          character_offset: 0,
          completed,
          theme,
          font_family: fontFamily,
          font_size: fontSize,
        }),
      })
        .then((response) => {
          if (!response.ok) setNotice("Reading position could not be saved.");
        })
        .catch(() => setNotice("Reading position could not be saved."));
    }, 650);
    return () => window.clearTimeout(timer);
  }, [completed, currentLocator, document, fontFamily, fontSize, publicationId, theme]);

  const scrollTo = useCallback((locator: string) => {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    documentLocator(locator)?.scrollIntoView({
      behavior: reducedMotion ? "auto" : "smooth",
      block: "start",
    });
    setCurrentLocator(locator);
    setNavigationOpen(false);
  }, []);

  const addBookmark = useCallback(async () => {
    if (!currentLocator || !document) return;
    const chapter = [...document.chapters]
      .reverse()
      .find(
        (item) =>
          document.blocks.findIndex((block) => block.locator === item.locator) <=
          document.blocks.findIndex((block) => block.locator === currentLocator),
      );
    const response = await fetch(`/api/reading-room/publications/${publicationId}/bookmarks`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        locator: currentLocator,
        character_offset: 0,
        label: chapter?.title ?? "Saved place",
      }),
    });
    if (response.ok) {
      const bookmark = (await response.json()) as ReaderBookmark;
      setBookmarks((items) => [...items, bookmark]);
      setNotice("Bookmark added.");
    } else setNotice("Bookmark could not be added.");
  }, [currentLocator, document, publicationId]);

  const removeBookmark = async (bookmark: ReaderBookmark) => {
    const response = await fetch(
      `/api/reading-room/publications/${publicationId}/bookmarks/${bookmark.id}`,
      { method: "DELETE", credentials: "include" },
    );
    if (response.ok) {
      setBookmarks((items) => items.filter((item) => item.id !== bookmark.id));
      setNotice("Bookmark removed.");
    } else setNotice("Bookmark could not be removed.");
  };

  useEffect(() => {
    const keyboard = (event: KeyboardEvent) => {
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement)
        return;
      if (event.key === "]") setFontSize((size) => Math.min(32, size + 1));
      if (event.key === "[") setFontSize((size) => Math.max(14, size - 1));
      if (event.key.toLowerCase() === "b") void addBookmark();
      if (event.key === "Escape") setNavigationOpen(false);
    };
    window.addEventListener("keydown", keyboard);
    return () => window.removeEventListener("keydown", keyboard);
  }, [addBookmark]);

  const captions = useMemo(
    () =>
      new Map(
        document?.blocks
          .filter((block) => block.block_type === "caption" && block.parent_locator)
          .map((block) => [block.parent_locator!, block.text]) ?? [],
      ),
    [document],
  );
  const palette = THEMES[theme];

  if (error)
    return (
      <div className="max-w-3xl mx-auto pv-panel p-8" role="alert">
        {error}
      </div>
    );
  if (!document)
    return <div className="max-w-3xl mx-auto pv-panel p-8 text-center">Opening reader…</div>;

  return (
    <div
      className="fixed inset-0 z-50 overflow-y-auto"
      style={{ background: palette.background, color: palette.text }}
    >
      <p className="sr-only" aria-live="polite">
        {notice}
      </p>
      <header
        className="sticky top-0 z-30 border-b px-3 py-2 backdrop-blur md:px-5"
        style={{ background: `${palette.page}f2`, borderColor: `${palette.muted}44` }}
      >
        <div className="mx-auto flex max-w-7xl items-center gap-1.5 sm:gap-2">
          <Link
            to="/app/reading-room"
            className="rounded-md p-2"
            aria-label="Return to Reading Room"
          >
            <ChevronLeft size={20} />
          </Link>
          <div className="hidden min-w-0 flex-1 sm:block">
            <div className="truncate text-sm font-semibold">{document.title}</div>
            <div className="truncate text-xs" style={{ color: palette.muted }}>
              {document.author}
            </div>
          </div>
          <button
            className="rounded-md p-2 md:hidden"
            onClick={() => setNavigationOpen((open) => !open)}
            aria-expanded={navigationOpen}
            aria-controls="reader-navigation"
            aria-label="Chapters and bookmarks"
          >
            <List size={19} />
          </button>
          <select
            aria-label="Reading theme"
            value={theme}
            onChange={(event) => setTheme(event.target.value as Theme)}
            className="rounded-md border bg-transparent px-2 py-1.5 text-xs"
            style={{ borderColor: `${palette.muted}66` }}
          >
            <option value="light">Light</option>
            <option value="dark">Dark</option>
            <option value="sepia">Sepia</option>
          </select>
          <select
            aria-label="Reading font"
            value={fontFamily}
            onChange={(event) => setFontFamily(event.target.value as FontFamily)}
            className="w-16 rounded-md border bg-transparent px-1 py-1.5 text-xs sm:w-auto sm:px-2"
            style={{ borderColor: `${palette.muted}66` }}
          >
            <option value="serif">Serif</option>
            <option value="sans">Sans</option>
          </select>
          <button
            onClick={() => setFontSize((size) => Math.max(14, size - 1))}
            className="rounded-md p-2"
            aria-label="Decrease text size"
          >
            <Minus size={16} />
          </button>
          <span className="hidden w-8 text-center text-xs sm:block" aria-live="polite">
            {fontSize}
          </span>
          <button
            onClick={() => setFontSize((size) => Math.min(32, size + 1))}
            className="rounded-md p-2"
            aria-label="Increase text size"
          >
            <Plus size={16} />
          </button>
          <button
            onClick={() => void addBookmark()}
            className="rounded-md p-2"
            aria-label="Bookmark current position"
          >
            <Bookmark size={18} />
          </button>
        </div>
      </header>

      <div className="mx-auto grid max-w-7xl md:grid-cols-[17rem_minmax(0,1fr)]">
        <ReaderNavigation
          document={document}
          bookmarks={bookmarks}
          open={navigationOpen}
          palette={palette}
          onSelect={scrollTo}
          onRemove={(bookmark) => void removeBookmark(bookmark)}
        />
        <main className="min-w-0 px-3 py-5 sm:px-6 md:py-8">
          <article
            lang={document.language ?? undefined}
            aria-label={`${document.title} by ${document.author}`}
            className="mx-auto max-w-[46rem] rounded-sm px-5 py-10 shadow-xl sm:px-10 md:px-14 md:py-14"
            style={{
              background: palette.page,
              color: palette.text,
              fontFamily:
                fontFamily === "serif"
                  ? "Georgia, 'Times New Roman', serif"
                  : "Inter, system-ui, sans-serif",
              fontSize: `${fontSize}px`,
              lineHeight: 1.72,
            }}
          >
            <header className="mb-14 text-center">
              <BookOpenText className="mx-auto mb-5" size={30} aria-hidden="true" />
              <h1 className="text-[1.8em] leading-tight">{document.title}</h1>
              <p className="mt-3 text-[0.9em]" style={{ color: palette.muted }}>
                {document.author}
              </p>
            </header>
            {document.blocks.map((block) => (
              <ReaderContentBlock
                key={block.locator}
                block={block}
                caption={captions.get(block.locator)}
                palette={palette}
              />
            ))}
            <footer
              className="mt-16 border-t pt-8 text-center"
              style={{ borderColor: `${palette.muted}44` }}
            >
              <button
                onClick={() => setCompleted(true)}
                className="inline-flex items-center gap-2 rounded-md border px-4 py-2 text-sm"
                style={{ borderColor: `${palette.muted}66` }}
              >
                <Check size={16} /> {completed ? "Completed" : "Mark as completed"}
              </button>
            </footer>
          </article>
        </main>
      </div>
    </div>
  );
}

function documentLocator(locator: string) {
  return document.querySelector<HTMLElement>(`[data-locator="${CSS.escape(locator)}"]`);
}

function ReaderContentBlock({
  block,
  caption,
  palette,
}: {
  block: ReaderBlock;
  caption?: string | null;
  palette: { text: string; muted: string };
}) {
  const common = {
    "data-locator": block.locator,
    id: `reader-${block.locator.replace(/[^A-Za-z0-9_-]/g, "-")}`,
    className: "scroll-mt-24",
  };
  if (block.block_type === "caption") return null;
  if (block.block_type === "page_marker")
    return (
      <span {...common} className={`${common.className} sr-only`}>
        Source page marker
      </span>
    );
  if (block.block_type === "illustration")
    return (
      <figure {...common} className={`${common.className} my-10`}>
        {block.illustration_url && (
          <img
            src={block.illustration_url}
            alt={caption ?? "Book illustration"}
            className="mx-auto max-h-[70vh] max-w-full object-contain"
            loading="lazy"
          />
        )}
        {caption && (
          <figcaption className="mt-3 text-center text-[0.82em]" style={{ color: palette.muted }}>
            {caption}
          </figcaption>
        )}
      </figure>
    );
  if (block.block_type === "footnote")
    return (
      <aside
        {...common}
        role="note"
        className={`${common.className} my-5 border-l-2 pl-4 text-[0.82em]`}
        style={{ borderColor: palette.muted, color: palette.muted }}
      >
        {block.text}
      </aside>
    );
  if (
    block.block_type === "part" ||
    block.block_type === "chapter" ||
    block.block_type === "heading"
  ) {
    const level = block.block_type === "part" ? 2 : block.block_type === "chapter" ? 2 : 3;
    return createElement(
      `h${level}`,
      { ...common, className: `${common.className} mb-5 mt-12 text-[1.45em] leading-tight` },
      block.text,
    );
  }
  if (block.block_type === "table")
    return (
      <div
        {...common}
        role="table"
        className={`${common.className} my-6 overflow-x-auto whitespace-pre-wrap border p-4 text-[0.9em]`}
        style={{ borderColor: `${palette.muted}66` }}
      >
        {block.text}
      </div>
    );
  return (
    <p {...common} className={`${common.className} my-5 text-justify [hyphens:auto]`}>
      {block.text}
    </p>
  );
}

function ReaderNavigation({
  document,
  bookmarks,
  open,
  palette,
  onSelect,
  onRemove,
}: {
  document: ReaderDocument;
  bookmarks: ReaderBookmark[];
  open: boolean;
  palette: { page: string; text: string; muted: string };
  onSelect: (locator: string) => void;
  onRemove: (bookmark: ReaderBookmark) => void;
}) {
  return (
    <aside
      id="reader-navigation"
      className={`${open ? "block" : "hidden"} fixed inset-x-3 top-16 z-20 max-h-[70vh] overflow-y-auto rounded-lg border p-4 shadow-xl md:sticky md:top-[57px] md:block md:max-h-[calc(100vh-57px)] md:rounded-none md:border-y-0 md:border-l-0 md:shadow-none`}
      style={{ background: palette.page, borderColor: `${palette.muted}44`, color: palette.text }}
      aria-label="Reader navigation"
    >
      <h2
        className="text-xs font-semibold uppercase tracking-wider"
        style={{ color: palette.muted }}
      >
        Chapters
      </h2>
      <ol className="mt-3 space-y-1">
        {document.chapters.map((chapter) => (
          <li key={chapter.locator}>
            <button
              onClick={() => onSelect(chapter.locator)}
              className="w-full rounded px-2 py-1.5 text-left text-sm"
              style={{ paddingLeft: `${Math.min(3, chapter.level - 1) * 0.65 + 0.5}rem` }}
            >
              {chapter.title}
            </button>
          </li>
        ))}
      </ol>
      <h2
        className="mt-7 text-xs font-semibold uppercase tracking-wider"
        style={{ color: palette.muted }}
      >
        Bookmarks
      </h2>
      {bookmarks.length ? (
        <ul className="mt-3 space-y-2">
          {bookmarks.map((bookmark) => (
            <li key={bookmark.id} className="flex items-center gap-1">
              <button
                onClick={() => onSelect(bookmark.locator)}
                className="min-w-0 flex-1 truncate rounded px-2 py-1.5 text-left text-sm"
              >
                {bookmark.label ?? "Saved place"}
              </button>
              <button
                onClick={() => onRemove(bookmark)}
                className="rounded p-1.5"
                aria-label={`Remove bookmark ${bookmark.label ?? "Saved place"}`}
              >
                <BookmarkMinus size={15} />
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-3 text-sm" style={{ color: palette.muted }}>
          No bookmarks yet.
        </p>
      )}
      <p className="mt-8 text-xs leading-relaxed" style={{ color: palette.muted }}>
        Keyboard: [ and ] change text size, B adds a bookmark, Escape closes navigation.
      </p>
    </aside>
  );
}
