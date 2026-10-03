import { useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "../ui/dialog";

export function HomeVideoMove({ assetId, onMoved }: { assetId: string; onMoved: () => void }) {
  const [open, setOpen] = useState(false);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [operation, setOperation] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const base = `/api/vault-master/assets/${assetId}/section-move`;
  useEffect(() => {
    if (!operation) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const check = async () => {
      try {
        const response = await fetch(`${base}/${operation}`, {
          credentials: "include",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error();
        const state = await response.json();
        if (controller.signal.aborted) return;
        if (state.status === "completed") {
          setOperation(null);
          setBusy(false);
          setOpen(false);
          onMoved();
        } else if (state.status === "failed") throw new Error();
        else timer = setTimeout(() => void check(), 2000);
      } catch {
        if (!controller.signal.aborted) {
          setBusy(false);
          setOperation(null);
          setMessage("The move needs recovery. Reopen this video to check its current state.");
        }
      }
    };
    void check();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [operation, base, onMoved]);
  const preflight = async () => {
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(`${base}/preflight`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ destination: "Music Videos" }),
      });
      if (!response.ok || !(await response.json()).ready) throw new Error();
      setReady(true);
    } catch {
      setMessage(
        "Music Videos is not currently an available destination. Hidden videos must be restored before moving.",
      );
    } finally {
      setBusy(false);
    }
  };
  const move = async () => {
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(base, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ destination: "Music Videos", confirm: true }),
      });
      if (!response.ok) throw new Error();
      setOperation((await response.json()).operation_id);
    } catch {
      setBusy(false);
      setReady(false);
      setMessage("The move could not be started.");
    }
  };
  return (
    <Dialog
      open={open}
      onOpenChange={(value) => {
        if (!busy) {
          setOpen(value);
          setReady(false);
          setMessage("");
        }
      }}
    >
      <DialogTrigger asChild>
        <button type="button" className="pv-btn-ghost min-h-11">
          Move
        </button>
      </DialogTrigger>
      <DialogContent className="pv-dialog max-h-[90dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Move to Music Videos</DialogTitle>
          <DialogDescription>
            The same video will leave Home Videos and appear in Music Videos. Ownership and current
            visibility stay unchanged.
          </DialogDescription>
        </DialogHeader>
        {message && <p role="alert">{message}</p>}
        <button
          type="button"
          disabled={busy}
          className="pv-btn-primary min-h-11"
          onClick={() => void (ready ? move() : preflight())}
        >
          {busy ? "Moving..." : ready ? "Confirm move" : "Choose Music Videos"}
        </button>
        <button
          type="button"
          disabled={busy}
          className="pv-btn-ghost min-h-11"
          onClick={() => setOpen(false)}
        >
          Cancel
        </button>
      </DialogContent>
    </Dialog>
  );
}
