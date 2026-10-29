// Все сетевые запросы идут отсюда, чтобы не упираться в CORS страницы.
const ALLOWED = ["https://api.demonlist.org/", "https://pointercrate.com/api/", "https://raw.githubusercontent.com/gl-ext/gl-ext/"];

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type !== "fetchJson") return;
  const url = String(msg.url || "");
  if (!ALLOWED.some((p) => url.startsWith(p))) {
    sendResponse({ ok: false, error: "url not allowed" });
    return;
  }
  fetch(url, { headers: { Accept: "application/json" }, credentials: "omit" })
    .then(async (r) => sendResponse({ ok: r.ok, status: r.status, data: r.ok ? await r.json() : null }))
    .catch((e) => sendResponse({ ok: false, error: String(e) }));
  return true; // ответ асинхронный
});