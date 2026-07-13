# Turning on the AI (free)

Unfog gets its "wow" — smart task breakdowns and energy-aware day plans — from an AI
provider. It works with a **free** one. Pick either (Gemini is the simplest); set one
env var on Render and you're done. No AI key? The app still works with sensible
rule-based steps, so this is optional.

Priority if more than one is set: **Gemini → Groq → Claude → rule-based fallback.**

## Option A — Google Gemini (recommended, free)
1. Go to **aistudio.google.com/apikey** (sign in with a Google account).
2. Click **Create API key** → copy it.
3. Render → your service → **Environment** → add:
   - `GEMINI_API_KEY` = *(the key)*
4. (optional) `GEMINI_MODEL` = `gemini-2.0-flash` (default; free-tier, fast).

Free tier limits are generous for a beta (plenty of requests/day). If you ever hit a
limit, Unfog just falls back to rule-based steps for that request — nothing breaks.

## Option B — Groq (free, very fast)
1. Go to **console.groq.com/keys** → sign up → **Create API Key** → copy.
2. Render → **Environment** → add:
   - `GROQ_API_KEY` = *(the key)*
3. (optional) `GROQ_MODEL` = `llama-3.3-70b-versatile` (default).

## Option C — Anthropic Claude (paid, best quality)
Set `ANTHROPIC_API_KEY` (+ optionally `ANTHROPIC_MODEL=claude-haiku-4-5` to keep it cheap).
~€5 of credit lasts 1000+ uses on Haiku.

## Verify
After adding the key, Render redeploys automatically. Open **/app/dump** — the little
"running without an AI key" note disappears when a provider is active. Do a brain dump:
the steps should now be specific to what you wrote (not the generic starter steps).

## Cost & safety notes
- Gemini and Groq free tiers cost **€0**. Rate limits apply; on a limit or any error,
  Unfog silently uses rule-based steps for that request.
- Keys are secrets — set them only as Render env vars, never commit them.
