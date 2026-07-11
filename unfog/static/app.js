// Unfog client bits: PWA registration, busy buttons, focus timer.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
  // When a new service worker takes control, reload once so the fresh version
  // (and its cache) is in charge — prevents an old cached worker lingering.
  let reloaded = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (reloaded) return;
    reloaded = true;
    window.location.reload();
  });
}

// show progress on slow submits (AI breakdown / day planning)
document.querySelectorAll("form").forEach((f) => {
  const b = f.querySelector("button[data-busy]");
  if (!b) return;
  f.addEventListener("submit", () => {
    b.textContent = b.dataset.busy;
    b.disabled = true;
  });
});

// push: turn on the daily nudge
const pushbtn = document.getElementById("pushbtn");
if (pushbtn && "serviceWorker" in navigator && "PushManager" in window) {
  const keyMeta = document.querySelector('meta[name="vapid-key"]');
  const b64ToUint8 = (s) => {
    const pad = "=".repeat((4 - (s.length % 4)) % 4);
    const b = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from([...b].map((c) => c.charCodeAt(0)));
  };
  pushbtn.addEventListener("click", async () => {
    if (!keyMeta) return;
    pushbtn.disabled = true;
    try {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { pushbtn.textContent = "Notifications blocked"; return; }
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: b64ToUint8(keyMeta.content),
      });
      const r = await fetch("/app/push/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sub),
      });
      pushbtn.textContent = r.ok ? pushbtn.dataset.on : "Try again";
    } catch (e) {
      pushbtn.textContent = "Try again";
      pushbtn.disabled = false;
    }
  });
}

// focus timer
const timer = document.getElementById("timer");
if (timer) {
  const ring = document.getElementById("ring");
  const clock = document.getElementById("clock");
  const startbtn = document.getElementById("startbtn");
  const doneform = document.getElementById("focusdone");
  const minutesdone = document.getElementById("minutesdone");
  let total = 10 * 60, left = total, tick = null;

  const fmt = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  const paint = () => {
    clock.textContent = fmt(left);
    ring.style.setProperty("--p", (100 * (total - left)) / total);
  };

  document.querySelectorAll("#presets button").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll("#presets button").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      total = left = parseInt(b.dataset.min, 10) * 60;
      paint();
    });
  });

  const chime = () => {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = 660;
      g.gain.setValueAtTime(0.001, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.2, ctx.currentTime + 0.05);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 1.2);
      o.connect(g).connect(ctx.destination);
      o.start(); o.stop(ctx.currentTime + 1.3);
    } catch (e) {}
  };

  startbtn.addEventListener("click", () => {
    if (tick) { // pause
      clearInterval(tick); tick = null;
      startbtn.textContent = "Resume";
      return;
    }
    startbtn.textContent = "Pause";
    tick = setInterval(() => {
      left -= 1;
      paint();
      if (left <= 0) {
        clearInterval(tick); tick = null;
        chime();
        startbtn.classList.add("hidden");
        minutesdone.value = Math.round(total / 60);
        doneform.classList.remove("hidden");
        document.title = "Done! — Unfog";
      }
    }, 1000);
  });
  paint();
}
