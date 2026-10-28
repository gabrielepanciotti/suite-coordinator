import { useState } from "react";
import { Button, Input, Field } from "@suite/design-system";
import { api } from "./api";
import { Select } from "./Select";
import { Toggle } from "./Toggle";
import { CronEditor } from "./CronEditor";

// Health's Slot vocabulary. "next" (stored as no slot) lets Health pick the
// next enabled slot: Radix refuses an item whose value is "".
const REGIME_SLOTS = [
  { value: "next", label: "Prossima fascia (decide Health)" },
  { value: "wake", label: "Risveglio" },
  { value: "breakfast", label: "Colazione" },
  { value: "mid_morning", label: "Metà mattina" },
  { value: "lunch", label: "Pranzo" },
  { value: "afternoon", label: "Pomeriggio" },
  { value: "dinner", label: "Cena" },
  { value: "bedtime", label: "Prima di dormire" },
];

function initForm(initial) {
  if (initial) {
    const isChat = initial.destination?.startsWith("chat:");
    return {
      title: initial.title || "",
      project: initial.params?.project || "",
      provider: initial.provider || "static",
      schedule: initial.schedule || "0 8 * * *",
      destination: isChat ? "chat" : initial.destination || "dm",
      params: { ...initial.params },
      recipients: [...(initial.recipients || [])],
      chatId: isChat ? initial.destination.slice(5) : "",
    };
  }
  return {
    title: "",
    project: "",
    provider: "static",
    schedule: "0 8 * * *",
    destination: "dm",
    params: {},
    recipients: [],
    chatId: "",
  };
}

export function NotificationForm({ meta, initial, instance, onSaved, onClose }) {
  const [form, setForm] = useState(() => initForm(initial));
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState(null);

  const set = (key, value) => setForm((f) => ({ ...f, [key]: value }));
  const setParam = (key, value) =>
    setForm((f) => ({ ...f, params: { ...f.params, [key]: value } }));

  // The node (project) a notification belongs to — chosen first, it drives which
  // providers are offered and which real group the destination can target.
  const projectOptions = (meta?.projects || []).map((p) => ({
    value: p.key,
    label: p.label,
  }));
  const nodeGroup = (meta?.groups || []).find(
    (g) => g.project === form.project,
  );
  const profileKeys = (meta?.profiles || []).map((p) => p.key);

  // Providers owned by a node (provider_scope) show only when that node is the
  // notification's target. Providers absent from the map are global.
  const providerScope = meta?.provider_scope || {};
  const providerOptions = (meta?.providers || [])
    .filter((p) => !providerScope[p] || providerScope[p] === form.project)
    .map((p) => ({ value: p, label: p }));

  // Destination options depend on the node: its real Telegram group (when it has
  // one), a direct message, or an explicit chat id for special cases.
  const destOptions = [
    { value: "dm", label: "Messaggio diretto (DM)" },
    ...(nodeGroup
      ? [{ value: "group", label: `Gruppo: ${nodeGroup.label}` }]
      : []),
    { value: "chat", label: "Altra chat (ID)" },
  ];

  // When the node changes, drop selections that no longer fit it.
  const onNodeChange = (nextProject) => {
    set("project", nextProject);
    const owner = providerScope[form.provider];
    if (owner && owner !== nextProject) {
      set("provider", "static");
      set("params", {});
    }
    const hasGroup = (meta?.groups || []).some(
      (g) => g.project === nextProject,
    );
    if (form.destination === "group" && !hasGroup) set("destination", "dm");
  };

  const buildPayload = () => {
    const payload = {
      title: form.title,
      provider: form.provider,
      schedule: form.schedule,
      recipients: form.destination === "dm" ? form.recipients : [],
      params: { ...form.params, project: form.project },
      destination: form.destination,
      // Operator contract (#78): the owning Life instance, required on write.
      instance: initial?.instance || instance || undefined,
    };
    if (form.destination === "chat") {
      payload.destination = `chat:${form.chatId}`;
    }
    return payload;
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!form.project) {
      setFormError("Scegli il nodo della notifica");
      return;
    }
    setSaving(true);
    setFormError(null);
    try {
      const payload = buildPayload();
      if (initial) {
        await api.update(initial.key, payload);
      } else {
        await api.create(payload);
      }
      onSaved();
    } catch (err) {
      setFormError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const toggleRecipient = (key) => {
    set(
      "recipients",
      form.recipients.includes(key)
        ? form.recipients.filter((r) => r !== key)
        : [...form.recipients, key],
    );
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={initial ? "Modifica notifica" : "Nuova notifica"}
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
    >
      <div
        className="absolute inset-0 bg-overlay backdrop-blur-sm"
        onClick={onClose}
      />
      <div className="relative z-10 w-full max-w-lg bg-surface border border-border rounded-2xl shadow-2xl overflow-hidden max-h-[90vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-border">
          <h2 className="font-semibold text-lg text-text">
            {initial ? "Modifica notifica" : "Nuova notifica"}
          </h2>
          <Button
            variant="ghost"
            size="sm"
            onClick={onClose}
            aria-label="Chiudi"
          >
            <svg
              aria-hidden="true"
              className="w-5 h-5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </Button>
        </div>
        {/* Body */}
        <form
          onSubmit={handleSubmit}
          className="overflow-y-auto flex-1 px-6 py-4 space-y-4"
        >
          {/* Nodo — prima scelta: definisce provider e destinazioni disponibili */}
          <Field label="Nodo" htmlFor="notif-project" required>
            <Select
              id="notif-project"
              value={form.project}
              options={projectOptions}
              onChange={onNodeChange}
              placeholder="Scegli il nodo…"
            />
          </Field>

          {/* Titolo */}
          <Field label="Titolo" htmlFor="notif-title" required>
            <Input
              id="notif-title"
              value={form.title}
              onChange={(e) => set("title", e.target.value)}
              required
              placeholder="Nome notifica"
            />
          </Field>

          {/* Provider */}
          <Field label="Provider" htmlFor="notif-provider">
            <Select
              id="notif-provider"
              value={form.provider}
              options={providerOptions}
              onChange={(v) => {
                set("provider", v);
                set("params", {});
              }}
            />
          </Field>

          {/* Campi dinamici provider */}
          {form.provider === "agent" && (
            <Field label="Prompt" htmlFor="notif-prompt">
              {/* Native until the vendored DS in the dev container ships Textarea. */}
              <textarea
                id="notif-prompt"
                value={form.params.prompt || ""}
                onChange={(e) => setParam("prompt", e.target.value)}
                rows={4}
                className="w-full bg-surface-2 border border-border rounded-lg px-3 py-2 text-sm text-text focus:outline-none focus-visible:ring-2 focus-visible:ring-ring resize-y"
                placeholder="Testo del prompt per Claude…"
              />
            </Field>
          )}
          {form.provider === "static" && (
            <Field label="Testo" htmlFor="notif-text">
              {/* Native until the vendored DS in the dev container ships Textarea. */}
              <textarea
                id="notif-text"
                value={form.params.text || ""}
                onChange={(e) => setParam("text", e.target.value)}
                rows={3}
                className="w-full bg-surface-2 border border-border rounded-lg px-3 py-2 text-sm text-text focus:outline-none focus-visible:ring-2 focus-visible:ring-ring resize-y"
                placeholder="Testo della notifica…"
              />
            </Field>
          )}

          {form.provider === "regime" && (
            <>
              <Field
                label="Persona (slug in Health)"
                htmlFor="notif-person"
                required
              >
                <Input
                  id="notif-person"
                  value={form.params.person || ""}
                  onChange={(e) => setParam("person", e.target.value.trim())}
                  required
                  placeholder="es. owner"
                />
              </Field>
              <Field label="Fascia oraria" htmlFor="notif-slot">
                <Select
                  id="notif-slot"
                  value={form.params.slot || "next"}
                  options={REGIME_SLOTS}
                  onChange={(v) => setParam("slot", v === "next" ? "" : v)}
                />
              </Field>
            </>
          )}

          {/* Destinazione */}
          <Field label="Destinazione" htmlFor="notif-destination">
            <Select
              id="notif-destination"
              value={form.destination}
              options={destOptions}
              onChange={(v) => set("destination", v)}
            />
          </Field>
          {form.destination === "chat" && (
            <Field label="Chat ID" htmlFor="notif-chat-id">
              <Input
                id="notif-chat-id"
                value={form.chatId}
                onChange={(e) => set("chatId", e.target.value)}
                className="font-mono"
                placeholder="-1001234567890"
              />
            </Field>
          )}

          {/* Recipients (solo DM) */}
          {form.destination === "dm" && profileKeys.length > 0 && (
            <div>
              <label className="text-xs text-muted mb-2 block">
                Destinatari
              </label>
              <div className="flex flex-wrap gap-3">
                {profileKeys.map((k) => (
                  <label
                    key={k}
                    className="flex items-center gap-2 cursor-pointer select-none"
                  >
                    <Toggle
                      label={`Destinatario ${k}`}
                      checked={form.recipients.includes(k)}
                      onChange={() => toggleRecipient(k)}
                    />
                    <span className="text-sm text-text">{k}</span>
                  </label>
                ))}
              </div>
            </div>
          )}

          {/* Schedule */}
          <div>
            <label className="text-xs text-muted mb-2 block">
              Schedulazione
            </label>
            <CronEditor
              value={form.schedule}
              onChange={(v) => set("schedule", v)}
            />
          </div>

          {/* Errore */}
          {formError && (
            <p className="text-danger text-sm whitespace-pre-line bg-danger-soft border border-danger/40 rounded-lg px-3 py-2">
              {formError}
            </p>
          )}
        </form>

        {/* Footer */}
        <div className="flex justify-end gap-2 px-6 py-4 border-t border-border">
          <Button type="button" variant="ghost" onClick={onClose}>
            Annulla
          </Button>
          <Button
            type="button"
            variant="primary"
            disabled={saving}
            loading={saving}
            onClick={handleSubmit}
          >
            {saving ? "Salvataggio…" : "Salva"}
          </Button>
        </div>
      </div>
    </div>
  );
}
