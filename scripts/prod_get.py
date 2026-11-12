#!/usr/bin/env python3
"""Read-only probe: GET a list of production paths and print a compact view.

Production is only reachable from GitHub Actions (the dev sandbox is blocked),
so this is the general-purpose window into it: no deploy needed to look at
something new, as long as an endpoint already serves it.

Usage: python3 scripts/prod_get.py "/api/stats" "/api/elo/summary" ...
Env:   PROBE_LIST_ITEMS (default 6)  how many items of a long list to print
       PROBE_MAX_CHARS  (default 6000) hard cap per response
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://beetingapp-1.onrender.com"
N_ITEMS = int(os.environ.get("PROBE_LIST_ITEMS", "6"))
MAX_CHARS = int(os.environ.get("PROBE_MAX_CHARS", "6000"))


def shrink(v, depth=0):
    """Trim long lists/strings so one response cannot flood the log."""
    if isinstance(v, list):
        head = [shrink(x, depth + 1) for x in v[:N_ITEMS]]
        if len(v) > N_ITEMS:
            head.append(f"... (+{len(v) - N_ITEMS} de {len(v)})")
        return head
    if isinstance(v, dict):
        return {k: shrink(x, depth + 1) for k, x in v.items()}
    if isinstance(v, str) and len(v) > 400:
        return v[:400] + f"... (+{len(v) - 400} chars)"
    return v


def main():
    for path in sys.argv[1:]:
        print("=" * 78)
        print(f"GET {path}")
        t0 = time.time()
        try:
            with urllib.request.urlopen(BASE + path, timeout=180) as r:
                raw = r.read()
                status = r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        except Exception as e:                        # noqa: BLE001 - diagnostics
            print(f"  ERRO: {type(e).__name__}: {e}")
            continue
        dt = time.time() - t0
        print(f"  HTTP {status} · {len(raw)} bytes · {dt:.1f}s")
        try:
            data = json.loads(raw)
        except Exception:
            print("  (nao e' JSON) " + raw[:300].decode("utf-8", "replace"))
            continue
        if isinstance(data, list):
            print(f"  lista com {len(data)} elemento(s)")
        out = json.dumps(shrink(data), ensure_ascii=False, indent=1, default=str)
        print(out[:MAX_CHARS] + ("\n  ... (cortado)" if len(out) > MAX_CHARS else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
