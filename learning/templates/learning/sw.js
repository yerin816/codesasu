{% load static %}// 코드사수 서비스 워커 (PWA 설치용 최소 버전)
// - 항상 인터넷에서 먼저 받고(네트워크 우선), 끊겼을 때만 저장해 둔 CSS·JS·아이콘을 쓴다.
//   그래서 코드를 고치면 바로 반영되고, 오래된 화면이 남는 문제가 없다.
// - API·로그인·관리 화면은 저장하지 않는다 (개인 기록과 정답 채점은 항상 서버에서).
const CACHE = "codesasu-static-v1";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function offlinePage() {
  const html = '<!doctype html><html lang="ko"><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<title>코드사수</title><body style="font-family:sans-serif;padding:32px 16px;line-height:1.6">' +
    '<h1 style="font-size:20px">인터넷 연결이 필요해요</h1>' +
    '<p>연결된 뒤 다시 열어 주세요. 푼 기록은 서버에 저장돼 있어요.</p></body></html>';
  return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8" } });
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (/^\/(api|accounts|admin)\//.test(url.pathname)) return;

  const isStatic = url.pathname.startsWith("/static/");
  event.respondWith(
    fetch(req)
      .then((res) => {
        if (isStatic && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((cache) => cache.put(req, copy));
        }
        return res;
      })
      .catch(async () => {
        const cached = await caches.match(req);
        if (cached) return cached;
        if (req.mode === "navigate") return offlinePage();
        return Response.error();
      })
  );
});

// ---------- 하루 1번 알림 (WBS 5.2, FR-14) ----------
// 서버가 암호화해 보낸 알림을 받아 화면에 띄운다. 누르면 코드사수를 연다
const ICON = "{% static 'learning/icons/icon-192.png' %}";

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "코드사수", {
      body: data.body || "",
      icon: ICON,
      tag: "codesasu-daily",  // 같은 날 알림이 여러 개 쌓이지 않게 하나로 바꿔 끼운다
      data: { url: data.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL((event.notification.data && event.notification.data.url) || "/", self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const win of wins) {
        if (new URL(win.url).origin === self.location.origin && "focus" in win) {
          return win.navigate(url).then((w) => (w || win).focus());
        }
      }
      return self.clients.openWindow(url);
    })
  );
});
