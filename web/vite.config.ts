import { defineConfig } from "vite";

// maplibre-gl loads its worker bundle via a relative `new URL(...)` at runtime; letting Vite's
// dependency optimizer pre-bundle/rewrite the package breaks that resolution (404s the worker
// in dev), so it's excluded here. See https://github.com/maplibre/maplibre-gl-js/issues (vite dev worker 404).
export default defineConfig({
  optimizeDeps: {
    exclude: ["maplibre-gl"],
  },
});
