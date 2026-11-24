import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [svelte()],
  base: "/static/app/",
  // Copying a public directory would put unhashed files beside the hashed ones.
  publicDir: false,
  build: {
    outDir: "../src/mailmind/web/static/app",
    emptyOutDir: true,
    manifest: "manifest.json",
    modulePreload: { polyfill: false },
    // The page's script policy forbids data: URLs.
    assetsInlineLimit: 0,
    rollupOptions: { input: ["src/bundle.js", "src/message.js"] },
  },
});
