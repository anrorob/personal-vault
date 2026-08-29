import { Link, Outlet, useNavigate, useRouterState } from "@tanstack/react-router";
import { useState } from "react";
import { useEffect } from "react";
import {
  ArrowLeft,
  Home,
  Film,
  Clapperboard,
  Images,
  FileText,
  Archive,
  BookOpenText,
  Inbox,
  Upload,
  Share2,
  LogOut,
  Menu,
  Music2,
  UserRound,
  Mail,
  ChevronDown,
  X,
  ShieldCheck,
  KeyRound,
} from "lucide-react";
import { PVLogo } from "./Logo";
import { getAuthSession, type AuthSession } from "@/lib/auth";

type NavItem = {
  to: string;
  label: string;
  icon: React.ComponentType<{ size?: number; className?: string }>;
};

const PRIMARY_NAV: NavItem[] = [
  { to: "/app", label: "Home", icon: Home },
  { to: "/app/gallery", label: "Gallery", icon: Images },
  { to: "/app/music", label: "Music", icon: Music2 },
  { to: "/app/movies", label: "Theatre", icon: Film },
  { to: "/app/personal-videos", label: "Home Videos", icon: Clapperboard },
  { to: "/app/reading-room", label: "Reading Room", icon: BookOpenText },
  { to: "/app/commons", label: "Vault Commons", icon: Share2 },
];

const SECONDARY_NAV: NavItem[] = [
  { to: "/app/security", label: "Security", icon: KeyRound },
  { to: "/app/people", label: "People", icon: UserRound },
  { to: "/app/email", label: "Email", icon: Mail },
  { to: "/app/documents", label: "Documents", icon: FileText },
  { to: "/app/archives", label: "Archives", icon: Archive },
  { to: "/app/ledger", label: "Ledger", icon: BookOpenText },
];

const SPECIAL_NAV: NavItem[] = [
  { to: "/app/arrival-hall", label: "Arrival Hall", icon: Inbox },
  { to: "/app/add", label: "Add to Vault", icon: Upload },
];

const VAULT_CONTROL_NAV: NavItem[] = [
  { to: "/app/vault-control/overview", label: "Overview", icon: Home },
  { to: "/app/vault-control/storage", label: "Storage", icon: Archive },
  { to: "/app/vault-control/intake", label: "Intake", icon: Inbox },
  { to: "/app/vault-control/recovery", label: "Catalogue Recovery", icon: ShieldCheck },
  { to: "/app/vault-control/services", label: "Vault Services", icon: ShieldCheck },
  { to: "/app/vault-control/users", label: "Users", icon: UserRound },
];

const TITLES: Record<string, { title: string; section: string }> = {
  "/app": { title: "Home", section: "Overview" },
  "/app/movies": { title: "Theatre", section: "Library" },
  "/app/personal-videos": { title: "Home Videos", section: "Library" },
  "/app/music": { title: "Music", section: "Library" },
  "/app/reading-room": { title: "Reading Room", section: "Library" },
  "/app/gallery": { title: "Gallery", section: "Library" },
  "/app/documents": { title: "Documents", section: "Library" },
  "/app/archives": { title: "Archives", section: "Library" },
  "/app/ledger": { title: "Ledger", section: "Financial Record" },
  "/app/arrival-hall": { title: "Arrival Hall", section: "Staging" },
  "/app/routing-memory": { title: "Routing Memory", section: "Vault Master" },
  "/app/add": { title: "Add to Vault", section: "Ingest" },
  "/app/commons": { title: "Vault Commons", section: "Sharing" },
  "/app/people": { title: "People", section: "" },
  "/app/email": { title: "Email", section: "" },
  "/app/security": { title: "Security", section: "" },
  "/app/vault-control": { title: "Vault Control", section: "Operations" },
  "/app/vault-control/overview": { title: "Overview", section: "Vault Control" },
  "/app/vault-control/storage": { title: "Storage", section: "Vault Control" },
  "/app/vault-control/intake": { title: "Intake", section: "Vault Control" },
  "/app/vault-control/recovery": { title: "Catalogue Recovery", section: "Vault Control" },
  "/app/vault-control/services": { title: "Vault Services", section: "Vault Control" },
  "/app/vault-control/users": { title: "Users", section: "Vault Control" },
};

export function AppShell() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const navigate = useNavigate();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [session, setSession] = useState<AuthSession | null>(null);
  const isVaultControl = pathname.startsWith("/app/vault-control");
  const secondaryRouteActive = SECONDARY_NAV.some(
    (item) => pathname === item.to || pathname.startsWith(`${item.to}/`),
  );
  const [moreOpen, setMoreOpen] = useState(
    () =>
      secondaryRouteActive ||
      (typeof window !== "undefined" && window.sessionStorage.getItem("pv-more-open") === "true"),
  );

  const meta = TITLES[pathname] ??
    Object.entries(TITLES).find(
      ([path]) => path !== "/app" && pathname.startsWith(`${path}/`),
    )?.[1] ?? { title: "Personal Vault", section: "" };
  const compactUsesSectionOnly = meta.section === "Library";

  const [isSigningOut, setIsSigningOut] = useState(false);

  useEffect(() => {
    void getAuthSession()
      .then(setSession)
      .catch(() => setSession(null));
  }, []);

  const displayName = session?.display_name ?? session?.username ?? "Vault user";
  const avatarInitial = displayName.trim().charAt(0).toUpperCase() || "?";
  const isAdministrator = session?.role === "administrator";

  useEffect(() => {
    if (typeof window !== "undefined") {
      window.sessionStorage.setItem("pv-more-open", String(moreOpen));
    }
  }, [moreOpen]);

  const signOut = async () => {
    if (isSigningOut) {
      return;
    }

    setIsSigningOut(true);

    try {
      const response = await fetch("/api/auth/logout", {
        method: "POST",
        credentials: "include",
      });

      if (!response.ok) {
        throw new Error("Logout failed");
      }

      await navigate({ to: "/login" });
    } catch {
      setIsSigningOut(false);
    }
  };

  return (
    <div className="pv min-h-screen flex">
      {/* Sidebar */}
      <aside
        data-mobile-open={mobileOpen}
        className={`fixed inset-y-0 left-0 z-40 w-64 flex-col border-r
          md:sticky md:top-0 md:h-screen md:self-start md:flex pv-sidebar ${mobileOpen ? "flex" : "hidden md:flex"}`}
        style={{
          background:
            "linear-gradient(180deg, rgba(215,185,104,0.025), transparent 28%), var(--pv-bg-elev)",
          borderColor: "var(--pv-border)",
        }}
      >
        <div
          className="flex items-center gap-3 px-5 py-5 border-b"
          style={{ borderColor: "var(--pv-border)" }}
        >
          <PVLogo size={32} />
          <div className="flex flex-col leading-tight">
            <span
              className="text-sm font-semibold tracking-wide"
              style={{ color: "var(--pv-silver)" }}
            >
              {isVaultControl ? "Vault Control" : "Personal Vault"}
            </span>
            <span
              className="text-[10px] uppercase tracking-widest"
              style={{ color: "var(--pv-gold)" }}
            >
              {isVaultControl ? "Administration" : "Self-hosted Vault"}
            </span>
          </div>
        </div>

        <nav className="flex-1 overflow-y-auto px-3 py-4 space-y-1">
          {(isVaultControl ? VAULT_CONTROL_NAV : PRIMARY_NAV).map((item) => {
            const active =
              pathname === item.to || (item.to !== "/app" && pathname.startsWith(`${item.to}/`));
            const Icon = item.icon;
            const navigationClassName = `pv-nav-item flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm ${active ? "pv-nav-item-active" : ""}`;
            const contents = (
              <>
                <Icon size={18} className="pv-nav-icon" />
                <span>{item.label}</span>
              </>
            );

            return (
              <Link
                key={item.to}
                to={item.to}
                onClick={() => setMobileOpen(false)}
                className={navigationClassName}
              >
                {contents}
              </Link>
            );
          })}
          {!isVaultControl && (
            <>
              <button
                type="button"
                className={`pv-nav-item flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-sm ${secondaryRouteActive ? "pv-nav-item-active" : ""}`}
                onClick={() => setMoreOpen((open) => !open)}
                aria-expanded={moreOpen}
                aria-controls="more-navigation"
              >
                <Archive size={18} className="pv-nav-icon" />
                <span className="flex-1 text-left">More</span>
                <ChevronDown
                  size={16}
                  className={`transition-transform ${moreOpen ? "rotate-180" : ""}`}
                  aria-hidden="true"
                />
              </button>
              {moreOpen && (
                <div id="more-navigation" className="space-y-1">
                  {SECONDARY_NAV.map((item) => {
                    const active = pathname === item.to || pathname.startsWith(`${item.to}/`);
                    const Icon = item.icon;
                    return (
                      <Link
                        key={item.to}
                        to={item.to}
                        onClick={() => setMobileOpen(false)}
                        className={`pv-nav-item flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm ${active ? "pv-nav-item-active" : ""}`}
                      >
                        <Icon size={18} className="pv-nav-icon" />
                        <span>{item.label}</span>
                      </Link>
                    );
                  })}
                </div>
              )}
              {SPECIAL_NAV.map((item) => {
                const active = pathname === item.to || pathname.startsWith(`${item.to}/`);
                const Icon = item.icon;
                return (
                  <Link
                    key={item.to}
                    to={item.to}
                    onClick={() => setMobileOpen(false)}
                    className={`pv-nav-item flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm ${active ? "pv-nav-item-active" : ""}`}
                  >
                    <Icon size={18} className="pv-nav-icon" />
                    <span>{item.label}</span>
                  </Link>
                );
              })}
            </>
          )}
        </nav>

        <div className="p-3 border-t" style={{ borderColor: "var(--pv-border)" }}>
          {isVaultControl ? (
            <Link
              to="/app"
              onClick={() => setMobileOpen(false)}
              className="pv-exit-vault pv-btn-ghost w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-colors"
            >
              <ArrowLeft size={18} />
              <span>Back to Vault</span>
            </Link>
          ) : (
            <button
              onClick={signOut}
              disabled={isSigningOut}
              className="pv-exit-vault pv-btn-ghost w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-colors"
            >
              <LogOut size={18} />
              <span>{isSigningOut ? "Exiting..." : "Exit The Vault"}</span>
            </button>
          )}
        </div>
      </aside>

      {mobileOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/60 md:hidden"
          data-mobile-navigation-overlay
          onClick={() => setMobileOpen(false)}
        />
      )}

      {/* Main */}
      <div className="flex-1 flex flex-col min-w-0">
        <header
          className="sticky top-0 z-20 flex shrink-0 items-center gap-4 px-5 md:px-8 h-16 border-b"
          style={{
            background:
              "linear-gradient(90deg, rgba(215,185,104,0.025), transparent 32%), rgba(14,14,17,0.96)",
            borderColor: "var(--pv-border)",
            backdropFilter: "blur(14px)",
          }}
        >
          <button
            className="md:hidden pv-mobile-nav-toggle pv-btn-ghost !px-2 !py-2"
            onClick={() => setMobileOpen((v) => !v)}
            aria-label="Toggle navigation"
          >
            {mobileOpen ? <X size={18} /> : <Menu size={18} />}
          </button>

          <div className="hidden md:block pv-desktop-header-logo">
            <PVLogo size={26} />
          </div>

          <div className="flex-1 min-w-0">
            <div className="flex items-baseline gap-3">
              <h1
                className={`pv-page-title text-base truncate ${compactUsesSectionOnly ? "hidden md:block" : ""}`}
              >
                {meta.title}
              </h1>
              {meta.section && (
                <span
                  className={`text-xs uppercase tracking-widest ${compactUsesSectionOnly ? "" : "hidden md:inline"}`}
                  style={{ color: "var(--pv-text-dim)" }}
                >
                  {meta.section}
                </span>
              )}
            </div>
          </div>

          <div
            className="flex items-center gap-3 pl-4 border-l"
            style={{ borderColor: "var(--pv-border)" }}
          >
            <div
              className="h-8 w-8 rounded-full flex items-center justify-center text-xs font-semibold"
              style={{
                background: "linear-gradient(180deg, #2a2b31, #16171b)",
                border: "1px solid var(--pv-border-strong)",
                color: "var(--pv-gold)",
              }}
            >
              {avatarInitial}
            </div>
            <span className="text-sm" style={{ color: "var(--pv-silver)" }}>
              {displayName}
            </span>
            {isVaultControl ? (
              <span
                className="hidden lg:inline text-xs uppercase tracking-widest"
                style={{ color: "var(--pv-gold)" }}
              >
                Control environment
              </span>
            ) : isAdministrator ? (
              <Link
                to="/app/vault-control/overview"
                className="pv-btn-ghost inline-flex items-center gap-2 !px-2.5 !py-2"
                aria-label="Open Vault Control"
              >
                <ShieldCheck size={17} />
                <span className="hidden lg:inline">Vault Control</span>
              </Link>
            ) : null}
          </div>
        </header>

        <main className="flex-1 px-5 md:px-8 py-8 pv-fade-in">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
