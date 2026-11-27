import adapter from "@sveltejs/adapter-static";
import { sveltekit } from "@sveltejs/kit/vite";
import { defineConfig } from "vite";

// The package serves this: index.html for every page, /_app for the rest.
const OUT = "../src/mailmind/web/static/ui";

export default defineConfig({
  plugins: [
    sveltekit({
      adapter: adapter({ pages: OUT, assets: OUT, fallback: "index.html" }),
      // SvelteKit boots from one inline script. Its hash goes into a meta policy here, and
      // the service copies it into its own header, which would otherwise forbid it.
      csp: { mode: "hash", directives: { "script-src": ["self"] } },
      // Nothing else polls: the event stream is the only connection a page keeps open.
      version: { pollInterval: 0 },
    }),
  ],
  build: {
    // The page's script policy forbids data: URLs.
    assetsInlineLimit: 0,
  },
});
