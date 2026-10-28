import { Button, Badge } from "@suite/design-system";
import { Toggle } from "./Toggle";

function fmt(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("it-IT", { dateStyle: "short", timeStyle: "short" });
}

export function NotificationCard({
  n,
  onToggle,
  onEdit,
  onTest,
  onPreview,
  onDelete,
}) {
  return (
    <div className="rounded-xl border border-border bg-surface p-4 flex flex-col gap-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-medium text-text">{n.title}</h3>
            {n.instance && <Badge>{n.instance}</Badge>}
            {n.origin === "manifest" && (
              <Badge state="warning">da progetto</Badge>
            )}
          </div>
          <p className="text-xs text-muted mt-0.5 break-words">
            {n.provider} · <code className="text-text">{n.schedule}</code> ·{" "}
            {n.destination}
          </p>
        </div>
        <Toggle
          checked={n.enabled}
          onChange={(v) => onToggle(n, v)}
          label={`Attiva notifica ${n.title}`}
        />
      </div>
      <div className="text-xs text-muted">
        Ultimo: {fmt(n.last_run)} · Prossimo: {fmt(n.next_run)}
      </div>
      {n.last_category === "broken" && n.last_outcome && (
        <div className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger whitespace-pre-wrap break-words">
          {n.last_outcome}
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={() => onEdit(n)}
        >
          Modifica
        </Button>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={() => onTest(n)}
        >
          Invia ora
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => onPreview(n)}
        >
          Anteprima
        </Button>
        <Button
          type="button"
          variant="destructive"
          size="sm"
          onClick={() => onDelete(n)}
        >
          Elimina
        </Button>
      </div>
    </div>
  );
}
