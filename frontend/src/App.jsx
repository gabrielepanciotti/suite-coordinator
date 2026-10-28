import { useMemo, useState } from "react";
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  useToast,
} from "@suite/design-system";
import { useNotifications } from "./useNotifications";
import { useCollapsedNodes } from "./useCollapsedNodes";
import { NotificationGroup } from "./NotificationGroup";
import { NotificationForm } from "./NotificationForm";
import { PreviewModal } from "./PreviewModal";
import { Select } from "./Select";
import { api } from "./api";

// Sentinella del selettore: "tutte" = nessun filtro d'istanza.
const ALL = "__all__";

// Raggruppa le notifiche per nodo canonico. L'API risolve già project (key
// canonica) e project_label (etichetta leggibile); meta è fallback storico.
function groupByNode(items, meta) {
  const labels = new Map((meta?.projects || []).map((p) => [p.key, p.label]));
  const groups = new Map();
  for (const n of items) {
    const key = n.project || "—";
    if (!groups.has(key)) {
      groups.set(key, {
        key,
        label: n.project_label || labels.get(key) || key,
        items: [],
      });
    }
    groups.get(key).items.push(n);
  }
  return [...groups.values()].sort((a, b) =>
    a.label.localeCompare(b.label, "it"),
  );
}

export default function App() {
  const [instance, setInstance] = useState(ALL);
  const selected = instance === ALL ? null : instance;
  const { items, meta, instances, defaultInstance, loading, error, reload } =
    useNotifications(selected);
  const [busy, setBusy] = useState(null);
  // editing: null = chiuso, { notif: null } = nuovo, { notif: n } = modifica
  const [editing, setEditing] = useState(null);
  const [previewing, setPreviewing] = useState(null);
  // Notifica di cui si chiede conferma d'eliminazione (null = nessun dialogo).
  const [deleting, setDeleting] = useState(null);
  const { toast } = useToast();
  const fail = (e) =>
    toast({ title: "Operazione non riuscita", description: e.message, state: "danger" });

  const onToggle = async (n, v) => {
    try {
      await api.update(n.key, { enabled: v, instance: n.instance });
      reload();
    } catch (e) {
      fail(e);
    }
  };
  const onDelete = (n) => setDeleting(n);
  const confirmDelete = async () => {
    const n = deleting;
    setDeleting(null);
    try {
      await api.remove(n.key);
      reload();
    } catch (e) {
      fail(e);
    }
  };
  const onTest = async (n) => {
    setBusy(n.key);
    try {
      await api.testNow(n.key);
      toast({ title: "Inviata.", state: "success" });
    } catch (e) {
      fail(e);
    } finally {
      setBusy(null);
    }
  };

  const { collapsed, toggle, collapseAll, expandAll } = useCollapsedNodes();
  const groups = useMemo(() => groupByNode(items, meta), [items, meta]);
  const allCollapsed =
    groups.length > 0 && groups.every((g) => collapsed.has(g.key));
  const handlers = {
    onToggle,
    onDelete,
    onTest,
    onEdit: (n) => setEditing({ notif: n }),
    onPreview: (n) => setPreviewing(n),
    busyKey: busy,
  };

  const instanceOptions = [
    { value: ALL, label: "Tutte le istanze" },
    ...instances.map((i) => ({
      value: i.id,
      label:
        i.status === "unreachable"
          ? `${i.id} · non raggiungibile`
          : i.id === defaultInstance
            ? `${i.id} · default`
            : i.id,
    })),
  ];
  const unreachable = instances.filter((i) => i.status === "unreachable");
  // La nuova notifica va all'istanza selezionata; in vista "tutte" al default.
  const formInstance =
    editing?.notif?.instance || (selected || defaultInstance || "");

  return (
    <div className="min-h-screen bg-bg text-text p-4 sm:p-6">
      <div className="flex items-center justify-between mb-6 gap-4 flex-wrap">
        <h1 className="text-2xl font-semibold">Notifiche Coordinator</h1>
        <div className="flex flex-wrap items-center gap-2">
          {instances.length > 0 && (
            <div className="w-full sm:w-52">
              <Select
                ariaLabel="Istanza"
                value={instance}
                options={instanceOptions}
                onChange={setInstance}
                placeholder="Istanza…"
              />
            </div>
          )}
          {groups.length > 0 && (
            <Button
              variant="secondary"
              size="sm"
              onClick={() =>
                allCollapsed
                  ? expandAll()
                  : collapseAll(groups.map((g) => g.key))
              }
            >
              {allCollapsed ? "Espandi tutti" : "Comprimi tutti"}
            </Button>
          )}
          <Button
            variant="primary"
            size="sm"
            onClick={() => setEditing({ notif: null })}
          >
            + Nuova notifica
          </Button>
        </div>
      </div>
      {unreachable.length > 0 && (
        <p className="mb-4 text-sm text-warning bg-warning-soft border border-warning/40 rounded-lg px-3 py-2">
          Istanze registrate ma non raggiungibili:{" "}
          {unreachable.map((i) => i.id).join(", ")}
        </p>
      )}
      {loading && <p className="text-muted">Caricamento…</p>}
      {error && <p className="text-danger whitespace-pre-line">{error}</p>}
      {!loading && !error && items.length === 0 && (
        <p className="text-muted">Nessuna notifica.</p>
      )}
      <div className="flex flex-col gap-4">
        {groups.map((g) => (
          <NotificationGroup
            key={g.key}
            nodeKey={g.key}
            label={g.label}
            items={g.items}
            collapsed={collapsed.has(g.key)}
            onToggle={toggle}
            handlers={handlers}
          />
        ))}
      </div>
      {editing !== null && (
        <NotificationForm
          meta={meta}
          initial={editing.notif}
          instance={formInstance}
          onSaved={() => {
            setEditing(null);
            reload();
          }}
          onClose={() => setEditing(null)}
        />
      )}
      {previewing !== null && (
        <PreviewModal draft={previewing} onClose={() => setPreviewing(null)} />
      )}
      <Dialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
      >
        <DialogContent mode="center">
          <DialogHeader>
            <DialogTitle>Eliminare la notifica?</DialogTitle>
            <DialogDescription>
              «{deleting?.title}» verrà eliminata e smetterà di essere inviata.
              L'operazione non si può annullare.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDeleting(null)}>
              Annulla
            </Button>
            <Button variant="destructive" onClick={confirmDelete}>
              Elimina
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
