import { existsSync } from "node:fs";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The animation lab (lab.html) is a page of the animation module: built only
// where that module is (src/animation/, left out of the open-source release).
const pages = { main: "index.html", ...(existsSync("lab.html") ? { lab: "lab.html" } : {}) };

// The server (port 8787) owns all data; the console is a static SPA.
const backend = "http://localhost:8787";

export default defineConfig(({ command }) => ({
  // Production build is mounted by the server at :8787/ui (server.py);
  // the dev server keeps root base with a proxy.
  base: command === "build" ? "/ui/" : "/",
  plugins: [react()],
  build: { rollupOptions: { input: pages } },
  server: {
    port: 5180,
    // Must cover every endpoint server.py registers: Vite
    // answers an unproxied path with index.html, so the fetch succeeds and the
    // feature silently does nothing — in `npm run dev` only.
    proxy: {
      "/api": backend, // sessions and turns (surfaces/console.py)
      "/usage_api": backend,
      "/auth_api": backend,
      "/slash-commands": backend,
      "/outputs": backend, // rendered diagrams and generated images in the thread
      "/inbox_api": backend,
      "/context_api": backend,
      "/learning_api": backend,
      "/claude_api": backend,
      "/feedback_api": backend,
    },
  },
}));
