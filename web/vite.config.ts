import { defineConfig, type Plugin } from "vite";
import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

// maplibre-gl's worker (maplibre-gl-worker.mjs) is *not* a self-contained bundle: as shipped in
// node_modules it does `import ... from "./maplibre-gl-shared.mjs"`, a plain relative specifier
// resolved against the worker script's own URL at runtime, so it only works when both files sit
// next to each other, unhashed, exactly as they do in node_modules/maplibre-gl/dist/.
//
// maplibre-gl resolves the worker's URL at runtime via `new URL('./maplibre-gl-worker.mjs',
// import.meta.url)` (see defaultWorkerUrl in the package) — i.e. relative to *our own bundle's*
// URL, not to the package. That's fine for `npm run dev` (Vite serves node_modules/ verbatim, so
// the derived URL happens to land on the real file — this is also why maplibre-gl is excluded
// from optimizeDeps below: letting esbuild pre-bundle/rewrite it would break that resolution) but
// wrong for `npm run build`: there's no node_modules-shaped path in dist/, so the derived URL
// 404s and maplibre silently never paints a single tile.
//
// Pointing maplibregl.setWorkerUrl() at a Vite `?url` import of the worker fixes *that* URL (Vite
// copies the file into dist/assets/ under a real, fetchable hashed name) but doesn't fix the
// worker's own internal import: the copy lands alone, without maplibre-gl-shared.mjs beside it,
// so the worker script loads and then immediately fails on its own `import ... from
// "./maplibre-gl-shared.mjs"` (a 404 that the dev/preview server's SPA fallback turns into an
// HTML response, which the browser rejects as "not a JavaScript module").
//
// So instead we copy *both* files, verbatim and un-hashed, into public/vendor/maplibre-gl/ (see
// copyMaplibreWorkerPlugin below) so the pair keeps referencing each other correctly wherever
// they're served from — public/ is served as-is by the dev server and copied as-is into dist/ by
// the build, so this works identically in `npm run dev`, `npm run build`, and `npm run preview`.
// MapView.ts then calls maplibregl.setWorkerUrl("/vendor/maplibre-gl/maplibre-gl-worker.mjs")
// before creating any Map. The copy is regenerated on every dev-server start / build (see below),
// so it never drifts from whatever maplibre-gl version is actually installed.
function copyMaplibreWorkerPlugin(): Plugin {
  const copy = () => {
    const srcDir = join(__dirname, "node_modules/maplibre-gl/dist");
    const destDir = join(__dirname, "public/vendor/maplibre-gl");
    mkdirSync(destDir, { recursive: true });
    for (const file of ["maplibre-gl-worker.mjs", "maplibre-gl-shared.mjs"]) {
      copyFileSync(join(srcDir, file), join(destDir, file));
    }
  };
  return {
    name: "copy-maplibre-worker",
    buildStart: copy,
  };
}

export default defineConfig({
  // Deployed under a sub-path on GitHub Pages (VITE_BASE=/demo-city/); "/" everywhere else.
  base: process.env.VITE_BASE ?? "/",
  plugins: [copyMaplibreWorkerPlugin()],
  optimizeDeps: {
    exclude: ["maplibre-gl"],
  },
});
