import { useEffect, useState } from "react";
import {
  Button,
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogClose,
} from "@suite/design-system";
import { api } from "./api";

export function PreviewModal({ draft, onClose }) {
  const [text, setText] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setText(null);
    api
      .preview(draft)
      .then((data) => {
        if (!cancelled) {
          setText(data.text || "");
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err.message);
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [draft]);

  return (
    <Dialog open={!!draft} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent mode="center">
        <DialogHeader>
          <DialogTitle>Anteprima notifica</DialogTitle>
        </DialogHeader>

        <div className="px-6 py-4 min-h-[120px]">
          {loading && (
            <div className="flex flex-col items-center justify-center gap-3 py-8">
              <div className="w-6 h-6 border-2 border-border border-t-accent rounded-full animate-spin" />
              <p className="text-sm text-muted text-center">
                Genero l'anteprima…
                <br />
                <span className="text-xs">
                  per le notifiche agente può richiedere un minuto
                </span>
              </p>
            </div>
          )}
          {error && (
            <p className="text-danger text-sm whitespace-pre-line bg-danger-soft border border-danger/40 rounded-lg px-3 py-2">
              {error}
            </p>
          )}
          {!loading && !error && (
            <div className="bg-surface-2 rounded-lg px-4 py-3">
              {text ? (
                <p className="text-sm text-text whitespace-pre-wrap">{text}</p>
              ) : (
                <p className="text-sm text-muted italic">(nessun testo)</p>
              )}
            </div>
          )}
        </div>

        <DialogFooter>
          <DialogClose asChild>
            <Button variant="ghost">Chiudi</Button>
          </DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
