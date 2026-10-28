import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Docker sets BACKEND_HOST=bot (the compose service/container DNS name) so
// the dev server proxies to the sibling container instead of its own
// loopback. Bare host-side `npm run dev` keeps talking to 127.0.0.1.
const backendHost = process.env.BACKEND_HOST || "127.0.0.1";
// FRONTEND_PORT lets Docker pin the dev server to the same port that is
// published on the host (single number, no host/container split). The bare
// fallback is the registry `front` port = 3333 + index 2; it used to be the
// legacy 3013, and a legacy default is exactly how the API ended up serving
// 8018 behind a publication that pointed elsewhere.
const port = Number(process.env.FRONTEND_PORT) || 3335;
// The API wants COORDINATOR_API_TOKEN (Coordinator#88). The proxy adds it on
// the server side, so the token never reaches the browser bundle.
const apiToken = process.env.COORDINATOR_API_TOKEN || "";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // The design system is a symlinked workspace package in dev: without dedupe
  // it brings its own React and every DS hook (useToast) fails.
  resolve: { dedupe: ["react", "react-dom"] },
  server: {
    port,
    host: true,
    strictPort: true,
    // Bind-mounted source on Docker Desktop needs polling to see host edits.
    watch: process.env.CHOKIDAR_USEPOLLING ? { usePolling: true } : undefined,
    // Coordinator registry `back` port = 9999 + index 2. Was 10027 (index 28's
    // port, held from an earlier index) — and nothing listened there.
    proxy: {
      "/api": {
        target: `http://${backendHost}:10001`,
        headers: apiToken ? { "X-Api-Token": apiToken } : {},
      },
    },
    // Vite answers 403 to any request whose Host header it does not recognise.
    // Behind the Life root Caddy the node vhost arrives as
    // coordinator.life.local, and in-network probes use the container name —
    // unlisted, both get 403 and the vhost serves nothing. Bare IPs (127.0.0.1)
    // are always allowed, which is why host-side access never showed this.
    // FRONTEND_ALLOWED_HOSTS (comma list, per device .env) adds the machine's own
    // name: on the minipc the Tailscale host `gab-server` got 403 while its IP worked.
    allowedHosts: [
      ".life.local",
      "coordinator-frontend",
      "localhost",
      ...(process.env.FRONTEND_ALLOWED_HOSTS || "").split(",").map((h) => h.trim()).filter(Boolean),
    ],
  },
});
