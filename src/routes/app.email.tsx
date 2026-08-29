import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/app/email")({ component: EmailPage });

function EmailPage() {
  return (
    <div className="max-w-6xl mx-auto space-y-8">
      <section>
        <h2 className="pv-content-title text-2xl tracking-tight md:text-3xl">Email</h2>
      </section>
      <section className="pv-panel px-6 py-12 text-center md:px-10 md:py-16">
        <p className="text-sm" style={{ color: "var(--pv-text-dim)" }}>
          Email is coming to Personal Vault.
        </p>
      </section>
    </div>
  );
}
