/* Service worker of the phone app (specs/mobile.md#phone-page § Offline). Served at
   /m/sw.js with scope /m; the route prepends VERSION (a hash of the shell files) and SHELL
   (the versioned URLs of the page and its static files). */

"use strict";

const PAGE = "/m";
const SHELL_CACHE = "anki-m-shell-" + VERSION;
const CDN_CACHE = "anki-m-cdn";
const MEDIA_CACHE = "anki-m-media"; // also filled by the page after each sync (mobile.js)
const PAGE_TIMEOUT_MS = 4000;

const MATHJAX_BASE = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/";
const MATHJAX_FONTS = [
  "Zero", "Main-Regular", "Main-Bold", "Main-Italic", "Math-Italic", "Math-BoldItalic",
  "Size1-Regular", "Size2-Regular", "Size3-Regular", "Size4-Regular", "AMS-Regular",
  "Calligraphic-Regular", "Fraktur-Regular", "SansSerif-Regular", "Script-Regular",
  "Typewriter-Regular", "Vector-Regular",
];
const MATHJAX = [MATHJAX_BASE + "tex-chtml.js"].concat(
  MATHJAX_FONTS.map((f) => MATHJAX_BASE + "output/chtml/fonts/woff-v2/MathJax_" + f + ".woff"),
);

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const shell = await caches.open(SHELL_CACHE);
      await shell.addAll(SHELL);
      // MathJax is best effort: the page works without it, math stays as source.
      const cdn = await caches.open(CDN_CACHE);
      await Promise.allSettled(
        MATHJAX.map(async (url) => {
          if (await cdn.match(url)) return;
          const response = await fetch(url, { mode: "cors" });
          if (response.ok) await cdn.put(url, response);
        }),
      );
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const name of await caches.keys()) {
        if (name.startsWith("anki-m-shell-") && name !== SHELL_CACHE) await caches.delete(name);
      }
      await self.clients.claim();
    })(),
  );
});

/* The page: network first so updates arrive, the cached copy when the Mac does not answer
   within PAGE_TIMEOUT_MS or the network fails. */
async function pageNetworkFirst(request) {
  const cache = await caches.open(SHELL_CACHE);
  const network = fetch(request).then((response) => {
    if (response.ok) cache.put(PAGE, response.clone());
    return response;
  });
  const cached = await cache.match(PAGE);
  if (!cached) return network;
  const timeout = new Promise((resolve) => setTimeout(() => resolve(cached), PAGE_TIMEOUT_MS));
  return Promise.race([network.catch(() => cached), timeout]);
}

async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request, { ignoreVary: true });
  if (hit) return hit;
  const response = await fetch(request);
  if (response.ok || response.type === "opaque") cache.put(request, response.clone());
  return response;
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin === self.location.origin) {
    if (request.mode === "navigate" || url.pathname === PAGE) {
      event.respondWith(pageNetworkFirst(request));
    } else if (url.pathname.startsWith("/api/mobile/media/")) {
      event.respondWith(cacheFirst(request, MEDIA_CACHE));
    } else if (url.pathname.startsWith("/static/") || url.pathname.startsWith("/m/")) {
      event.respondWith(cacheFirst(request, SHELL_CACHE));
    }
    // Anything else (the batch, the sync) goes to the network untouched.
    return;
  }
  if (url.hostname === "cdn.jsdelivr.net") event.respondWith(cacheFirst(request, CDN_CACHE));
});
