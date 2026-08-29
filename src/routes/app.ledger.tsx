import { createFileRoute } from "@tanstack/react-router";
import { BookOpenText } from "lucide-react";

export const Route = createFileRoute("/app/ledger")({
  component: LedgerPage,
});

function LedgerPage() {
  return (
    <div className="max-w-6xl mx-auto space-y-8">
      <section>
        <h2 className="pv-content-title text-2xl tracking-tight md:text-3xl">Ledger</h2>
        <p className="mt-2 text-sm" style={{ color: "var(--pv-text-dim)" }}>
          Your complete financial record, watched over by Vault Master.
        </p>
      </section>

      <section className="pv-panel px-6 py-12 md:px-10 md:py-16">
        <div className="mx-auto max-w-2xl text-center">
          <div
            className="mx-auto mb-6 flex h-14 w-14 items-center justify-center rounded-full"
            style={{
              color: "var(--pv-gold)",
              border: "1px solid var(--pv-border)",
              backgroundColor: "var(--pv-bg-elev)",
            }}
          >
            <BookOpenText size={24} />
          </div>

          <h3 className="text-lg font-semibold" style={{ color: "var(--pv-silver)" }}>
            Ledger is planned for a later stage of Personal Vault development.
          </h3>
          <p
            className="mx-auto mt-3 max-w-xl text-sm leading-6"
            style={{ color: "var(--pv-text-dim)" }}
          >
            It will combine transactions from all supported bank accounts, help track spending and
            regular payments, and allow Vault Master to identify anything that may need attention.
          </p>
        </div>
      </section>
    </div>
  );
}
