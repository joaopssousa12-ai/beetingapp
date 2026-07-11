# Turning on the daily nudge (web push)

Push is fully built but **off by default** so a build hiccup can never take the app
down. Turning it on is 3 steps. It stays off (and the app works perfectly) until
all three are done.

## 1. Add the push library to the build
In the Render service → **Settings → Build Command**, change it to:

```
pip install -r requirements.txt -r requirements-push.txt
```

(That installs `pywebpush`. If the build ever fails on it, revert to just
`pip install -r requirements.txt` and push stays off — nothing else breaks.)

## 2. Set the VAPID keys (env vars)
A key pair was generated for this project. In Render → **Environment**, add:

| Key | Value |
|---|---|
| `VAPID_PUBLIC_KEY` | *(the public key — safe to expose)* |
| `VAPID_PRIVATE_KEY` | *(the private key — keep secret, never commit)* |
| `VAPID_SUBJECT` | `mailto:youremail@example.com` |
| `CRON_TOKEN` | any random string (used to protect the reminder endpoint) |

To generate a fresh pair yourself any time:
```
pip install py-vapid && vapid --gen && vapid --applicationServerKey
```

## 3. Schedule the daily reminder (free)
Render's free tier has no built-in scheduler, so use a free external cron:
1. Go to **cron-job.org** (free), create a job.
2. URL: `https://unfog.onrender.com/tasks/remind?token=YOUR_CRON_TOKEN`
3. Schedule: once a day, e.g. 09:00 (this also keeps the free instance awake).

The endpoint sends a gentle nudge **only to users who haven't done a step yet that
day**, and auto-prunes dead subscriptions. It returns `{"sent": N, "candidates": N}`.

## How users turn it on
Once enabled, a **"🔔 Turn on a daily nudge"** button appears on the Wins page.
Tapping it asks for notification permission and subscribes that device. iOS note:
web push requires the user to **Add to Home Screen** first (install the PWA), then
enable notifications from inside the installed app.

## Verify
- With keys set + lib installed: `/app/push/subscribe` accepts a subscription;
  hitting the cron URL with the right token returns a JSON count.
- Test the actual delivery on a real phone (subscribe, then trigger the cron URL).
