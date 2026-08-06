"""
Qwen Coder API - Security Test Script

Tests:
  1. Protected routes reject requests with no API key (401)
  2. Protected routes reject requests with a WRONG key (401) - this also
     re-confirms the substring-bypass bug is actually fixed (see test 3)
  3. A key that's a SUBSTRING of the real one is rejected (401) - this is
     the specific bug we found and fixed (the old `in` comparison would
     have let this one through)
  4. Protected routes accept requests with the CORRECT key (200)
  5. Rate limiting kicks in after RATE_LIMIT requests within 60s (429)
  6. Flags routes that are currently unprotected (/add_project, /cancel)
     so you don't forget they're still open

Usage:
    python test_security.py --url http://localhost:8080 --key YOUR_REAL_API_KEY

Or, to read the key from your .env file automatically:
    pip install python-dotenv
    python test_security.py --url http://localhost:8080
"""

import argparse
import requests

try:
    from dotenv import load_dotenv
    import os
    load_dotenv()
except ImportError:
    pass


PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"


def check(label: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    return condition


def test_no_key(base_url: str):
    print("\n1. Requests with NO API key should be rejected (401)")
    r = requests.post(f"{base_url}/add_project", json={"project": "test_no_key"})
    if r.status_code == 401:
        check("/add_project rejects missing key", True)
    else:
        print(f"  [{WARN}] /add_project has no auth protection yet (got {r.status_code}, not 401) — known open item")

    r = requests.post(f"{base_url}/chat", json={"project": "default", "message": "hi"})
    check("/chat rejects missing key", r.status_code == 401, f"got {r.status_code}")


def test_wrong_key(base_url: str, real_key: str):
    print("\n2. Requests with a WRONG key should be rejected (401)")
    r = requests.post(
        f"{base_url}/chat",
        json={"project": "default", "message": "hi"},
        headers={"X-API-Key": "definitely-not-the-real-key"},
    )
    check("/chat rejects wrong key", r.status_code == 401, f"got {r.status_code}")


def test_substring_bug(base_url: str, real_key: str):
    print("\n3. A SUBSTRING of the real key should be rejected (the original bug)")
    if len(real_key) < 4:
        print(f"  [{WARN}] Real key too short to meaningfully test substring bypass, skipping")
        return
    substring_key = real_key[: max(3, len(real_key) // 3)]
    r = requests.post(
        f"{base_url}/chat",
        json={"project": "default", "message": "hi"},
        headers={"X-API-Key": substring_key},
    )
    check(
        "/chat rejects a substring of the real key",
        r.status_code == 401,
        f"sent '{substring_key}...', got {r.status_code} (200 here would mean the old bug is back)",
    )


def test_correct_key(base_url: str, real_key: str):
    print("\n4. Requests WITH the correct key should succeed")
    r = requests.post(f"{base_url}/add_project", json={"project": "test_correct_key"})
    r2 = requests.post(
        f"{base_url}/chat",
        json={"project": "test_correct_key", "message": "Say hello in one word."},
        headers={"X-API-Key": real_key},
    )
    check("/chat accepts the correct key", r2.status_code == 200, f"got {r2.status_code}: {r2.text[:200]}")


def test_rate_limit(base_url: str, real_key: str, rate_limit: int):
    print(f"\n5. Rate limiting should trigger after {rate_limit} requests/minute")
    hit_429 = False
    for i in range(rate_limit + 3):
        r = requests.post(
            f"{base_url}/search_web",
            json={"query": "test"},
            headers={"X-API-Key": real_key},
        )
        if r.status_code == 429:
            hit_429 = True
            check("Rate limit triggered", True, f"hit 429 on request #{i + 1}")
            break
    if not hit_429:
        check("Rate limit triggered", False, f"never got a 429 after {rate_limit + 3} requests")


def test_open_routes(base_url: str):
    print("\n6. Checking currently-unprotected routes (informational, not pass/fail)")
    r = requests.post(f"{base_url}/cancel")
    if r.status_code != 401:
        print(f"  [{WARN}] /cancel has no auth protection (got {r.status_code}) — decide if this needs one")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8080", help="Base URL of the running app")
    parser.add_argument("--key", default=None, help="Your real API_KEY (or set it in .env)")
    parser.add_argument("--rate-limit", type=int, default=10, help="Expected RATE_LIMIT value from app.py")
    args = parser.parse_args()

    real_key = args.key or os.getenv("API_KEY")
    if not real_key:
        raise SystemExit("No API key provided. Use --key YOUR_KEY or set API_KEY in a .env file.")

    print(f"=== Testing {args.url} ===")
    test_no_key(args.url)
    test_wrong_key(args.url, real_key)
    test_substring_bug(args.url, real_key)
    test_correct_key(args.url, real_key)
    test_rate_limit(args.url, real_key, args.rate_limit)
    test_open_routes(args.url)
    print("\nDone.")