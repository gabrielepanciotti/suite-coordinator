// Base "" => same-origin: passa dal proxy Vite (/api -> :10001), che aggiunge
// X-Api-Token lato server (Coordinator#88): il token non sta nel bundle.
const BASE = "";

async function req(method, path, body) {
  const res = await fetch(BASE + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.errors
      ? data.errors.join("\n")
      : data.detail || res.statusText;
    throw new Error(msg);
  }
  return data;
}

export const api = {
  // `instance` = null -> every instance (vista "tutte").
  list: (instance) =>
    req(
      "GET",
      instance
        ? `/api/notifications?instance=${encodeURIComponent(instance)}`
        : "/api/notifications",
    ),
  instances: () => req("GET", "/api/instances"),
  meta: () => req("GET", "/api/meta"),
  create: (n) => req("POST", "/api/notifications", n),
  update: (key, patch) => req("PATCH", `/api/notifications/${key}`, patch),
  remove: (key) => req("DELETE", `/api/notifications/${key}`),
  testNow: (key) => req("POST", `/api/notifications/${key}/test`),
  preview: (draft) => req("POST", "/api/notifications/preview", draft),
};
