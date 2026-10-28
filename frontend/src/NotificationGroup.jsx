import { Badge, Button } from "@suite/design-system";
import { NotificationCard } from "./NotificationCard";

export function NotificationGroup({
  nodeKey,
  label,
  items,
  collapsed,
  onToggle,
  handlers,
}) {
  const enabled = items.filter((n) => n.enabled).length;

  return (
    <section className="rounded-2xl border border-border bg-bg overflow-hidden">
      <Button
        type="button"
        variant="ghost"
        onClick={() => onToggle(nodeKey)}
        aria-expanded={!collapsed}
        aria-label={`${collapsed ? "Espandi" : "Comprimi"} gruppo ${label}`}
        className="h-auto w-full justify-start gap-3 rounded-none px-4 py-3 text-left"
      >
        <svg
          aria-hidden="true"
          className={`w-4 h-4 text-muted shrink-0 transition-transform duration-200 ${
            collapsed ? "-rotate-90" : "rotate-0"
          }`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M19 9l-7 7-7-7"
          />
        </svg>
        <h2 className="font-semibold text-text">{label}</h2>
        <Badge>
          {enabled}/{items.length}
        </Badge>
      </Button>
      {!collapsed && (
        <div className="grid gap-3 p-4 pt-0 sm:grid-cols-2 lg:grid-cols-3">
          {items.map((n) => (
            <NotificationCard
              key={n.key}
              n={n}
              onToggle={handlers.onToggle}
              onDelete={handlers.onDelete}
              onTest={handlers.onTest}
              onEdit={handlers.onEdit}
              onPreview={handlers.onPreview}
              busy={handlers.busyKey === n.key}
            />
          ))}
        </div>
      )}
    </section>
  );
}
