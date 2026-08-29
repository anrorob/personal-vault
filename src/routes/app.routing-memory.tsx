import { createFileRoute, Link } from "@tanstack/react-router";
import { BrainCircuit, Power, RefreshCcw, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { RoutingMemoryListing, RoutingMemoryRule } from "../lib/incoming";

export const Route = createFileRoute("/app/routing-memory")({ component: RoutingMemoryPage });

function RoutingMemoryPage() {
  const [rules, setRules] = useState<RoutingMemoryRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/vault-master/routing-memory", { credentials: "include" });
      if (!response.ok) throw new Error("Routing memory is unavailable");
      setRules(((await response.json()) as RoutingMemoryListing).rules);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Routing memory is unavailable");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => void load(), [load]);

  const update = async (
    rule: RoutingMemoryRule,
    action: "enable" | "disable" | "reset" | "edit",
    destination?: string,
  ) => {
    const response = await fetch(`/api/vault-master/routing-memory/${rule.id}`, {
      method: "PATCH",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, destination }),
    });
    if (!response.ok) return setError("The routing rule could not be updated.");
    await load();
  };

  const editDestination = async (rule: RoutingMemoryRule) => {
    const destination = window.prompt(
      "New destination: Gallery, Home Videos, Music, Movies, TV Shows, Documents, Archives, or Ledger",
      rule.destination,
    );
    if (!destination || destination === rule.destination) return;
    await update(rule, "edit", destination);
  };

  const remove = async (rule: RoutingMemoryRule) => {
    if (!window.confirm(`Forget the learned ${rule.destination} rule and all of its examples?`))
      return;
    const response = await fetch(`/api/vault-master/routing-memory/${rule.id}`, {
      method: "DELETE",
      credentials: "include",
    });
    if (!response.ok) return setError("The routing rule could not be deleted.");
    await load();
  };

  return (
    <main className="mx-auto w-full max-w-6xl px-5 py-10 md:px-10">
      <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="pv-display text-2xl text-[var(--pv-gold)]">The Vault Master presents</p>
          <h1 className="pv-display mt-2 text-4xl text-[var(--pv-gold-bright)]">Routing Memory</h1>
          <p className="mt-3 max-w-3xl text-[var(--pv-muted)]">
            Private, owner-scoped patterns learned from decisions you reviewed. Florence remains
            evidence; these rules only improve suggestions and never move a file automatically.
          </p>
        </div>
        <button className="pv-button-secondary flex items-center gap-2" onClick={() => void load()}>
          <RefreshCcw size={17} /> Refresh memory
        </button>
      </header>

      {error && (
        <div className="mb-5 rounded-xl border border-red-900/60 bg-red-950/25 p-4 text-red-200">
          {error}
        </div>
      )}
      {loading ? (
        <p className="text-[var(--pv-muted)]">Opening the private routing memory…</p>
      ) : rules.length === 0 ? (
        <section className="pv-card p-8 text-center">
          <BrainCircuit className="mx-auto mb-4 text-[var(--pv-gold)]" size={34} />
          <h2 className="text-xl text-[var(--pv-silver)]">No learned patterns yet</h2>
          <p className="mx-auto mt-2 max-w-xl text-[var(--pv-muted)]">
            Analyse and review files in the Arrival Hall. A pattern becomes a suggestion after three
            consistent examples.
          </p>
          <Link to="/app/arrival-hall" className="pv-button-primary mt-5 inline-flex">
            Open Arrival Hall
          </Link>
        </section>
      ) : (
        <div className="grid gap-5">
          {rules.map((rule) => (
            <article key={rule.id} className="pv-card p-6">
              <div className="flex flex-wrap justify-between gap-5">
                <div>
                  <p className="text-xs uppercase tracking-[0.2em] text-[var(--pv-gold)]">
                    {rule.maturity}
                  </p>
                  <h2 className="mt-2 text-2xl text-[var(--pv-silver)]">
                    Route to {rule.destination}
                  </h2>
                  <p className="mt-2 text-sm text-[var(--pv-muted)]">
                    {rule.example_count} supporting examples · {rule.contradiction_count}{" "}
                    contradictions · {Math.round(rule.confidence * 100)}% learned confidence
                  </p>
                </div>
                <span
                  className={`h-fit rounded-full border px-3 py-1 text-xs uppercase tracking-wider ${rule.status === "enabled" ? "border-[var(--pv-gold-dim)] text-[var(--pv-gold)]" : "border-[var(--pv-border)] text-[var(--pv-muted)]"}`}
                >
                  {rule.status}
                </span>
              </div>
              <dl className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {Object.entries(rule.features).map(([name, value]) => (
                  <div
                    key={name}
                    className="rounded-lg border border-[var(--pv-border)] bg-black/15 px-3 py-2"
                  >
                    <dt className="text-xs uppercase tracking-wider text-[var(--pv-muted)]">
                      {name.replaceAll("_", " ")}
                    </dt>
                    <dd className="mt-1 text-sm text-[var(--pv-silver)]">{value}</dd>
                  </div>
                ))}
              </dl>
              <p className="mt-4 text-sm text-[var(--pv-muted)]">
                Currently affects {rule.affected_item_ids.length} staged file
                {rule.affected_item_ids.length === 1 ? "" : "s"}.
              </p>
              <div className="mt-5 flex flex-wrap gap-3">
                <button
                  className="pv-button-secondary flex items-center gap-2"
                  onClick={() =>
                    void update(rule, rule.status === "enabled" ? "disable" : "enable")
                  }
                >
                  <Power size={16} /> {rule.status === "enabled" ? "Disable" : "Enable"}
                </button>
                <button className="pv-button-secondary" onClick={() => void update(rule, "reset")}>
                  Reset evidence
                </button>
                <button className="pv-button-secondary" onClick={() => void editDestination(rule)}>
                  Edit destination
                </button>
                <button
                  className="pv-button-secondary flex items-center gap-2"
                  onClick={() => void remove(rule)}
                >
                  <Trash2 size={16} /> Forget rule
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
    </main>
  );
}
