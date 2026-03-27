// protoResearcher — service worker (offline fallback)

const CACHE_NAME = "protoresearcher-v1";

const OFFLINE_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>protoResearcher — Offline</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #0a0f14;
      color: #e2e8f0;
      font-family: system-ui, -apple-system, sans-serif;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      flex-direction: column;
      gap: 24px;
    }
    .card {
      background: #0f1620;
      border: 1px solid rgba(20, 184, 166, 0.3);
      border-radius: 16px;
      padding: 40px 48px;
      text-align: center;
      max-width: 420px;
      width: 90%;
    }
    .logo {
      width: 56px;
      height: 56px;
      background: #14b8a6;
      border-radius: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 28px;
      font-weight: 700;
      color: white;
      margin: 0 auto 20px;
    }
    h1 { color: #5eead4; font-size: 22px; margin-bottom: 8px; }
    p { color: #94a3b8; font-size: 14px; line-height: 1.6; }
    .brand {
      color: #14b8a6;
      font-size: 12px;
      margin-top: 20px;
      opacity: 0.8;
    }
    .retry-btn {
      margin-top: 24px;
      padding: 10px 24px;
      background: #14b8a6;
      color: white;
      border: none;
      border-radius: 8px;
      font-size: 14px;
      cursor: pointer;
      transition: background 0.2s;
    }
    .retry-btn:hover { background: #0d9488; }
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">R</div>
    <h1>protoResearcher is offline</h1>
    <p>Check your connection and try again.</p>
    <button class="retry-btn" onclick="window.location.reload()">Retry</button>
    <p class="brand">protoLabs.studio</p>
  </div>
</body>
</html>`;

self.addEventListener("install", (e) => {
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(clients.claim());
});

self.addEventListener("fetch", (e) => {
  if (e.request.mode === "navigate") {
    e.respondWith(
      fetch(e.request).catch(
        () =>
          new Response(OFFLINE_HTML, {
            headers: { "Content-Type": "text/html; charset=utf-8" },
          }),
      ),
    );
  }
});
