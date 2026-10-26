"""One-time (re-runnable) setup: Cloudflare Worker + D1, GitHub repo + secrets, Composio Gmail link,
Instagram reading tokens copied from Naya News Autopilot (Project14).

Reads keys from ../.env (missing ones are taken from ../../Project14/.env and ~/.composio), never prints them.
Requires: `npx wrangler login` done, PyNaCl + requests installed.
Usage: python scripts/setup.py [--skip-github] [--skip-worker]
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import subprocess
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
WORKER = ROOT / "worker"
ENV_FILE = ROOT / ".env"
P14 = ROOT.parent / "Project14"
REPO_NAME = "news-digest-autopilot"
DB_NAME = "news-digest-db"
GH_API = "https://api.github.com"
COMPOSIO = "https://backend.composio.dev"
IG_KEYS = ["fb_app_secret", "fb_token", "fb_user_token", "fb_user_expires", "fb_ig_id", "fb_ig_username", "fb_page_name"]
WORKER_SECRETS = ["DASHBOARD_PASSWORD", "PIPELINE_SECRET", "GH_TOKEN", "COMPOSIO_API_KEY", "COMPOSIO_ORG_ID",
                  "COMPOSIO_PROJECT_ID", "COMPOSIO_USER_ID", "COMPOSIO_ACCOUNT_ID"]


def read_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
            if m:
                env[m.group(1)] = m.group(2).strip()
    return env


def save_env_value(key: str, value: str) -> None:
    text = ENV_FILE.read_text() if ENV_FILE.exists() else "# News Digest Autopilot keys (git-ignored)\n"
    if re.search(rf"^{key}=", text, re.M):
        text = re.sub(rf"^{key}=.*$", lambda _m: f"{key}={value}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    ENV_FILE.write_text(text)
    ENV_FILE.chmod(0o600)


def composio_env() -> dict:
    """Composio Gmail: the CLI's user key, its consumer project and the first active Gmail connection."""
    data = json.loads((Path.home() / ".composio" / "user_data.json").read_text())
    key, org = data["api_key"], data["org_id"]
    h = {"x-user-api-key": key, "x-org-id": org}
    proj = requests.post(f"{COMPOSIO}/api/v3/org/consumer/project/resolve", headers=h, json={}, timeout=30)
    proj.raise_for_status()
    p = proj.json()
    h["x-project-id"] = p["project_nano_id"]
    accts = requests.get(f"{COMPOSIO}/api/v3/connected_accounts", headers=h, timeout=30,
                         params={"toolkit_slugs": "gmail", "statuses": "ACTIVE", "limit": 20})
    accts.raise_for_status()
    active = [a for a in accts.json().get("items", []) if a.get("status") == "ACTIVE"]
    if not active:
        raise SystemExit("No active Gmail connection in Composio. Run `composio link gmail` first.")
    return {"COMPOSIO_API_KEY": key, "COMPOSIO_ORG_ID": org, "COMPOSIO_PROJECT_ID": p["project_nano_id"],
            "COMPOSIO_USER_ID": p["consumer_user_id"], "COMPOSIO_ACCOUNT_ID": active[0]["id"]}


def load_env() -> dict:
    env = read_env(ENV_FILE)
    p14 = read_env(P14 / ".env")
    for k in ("GEMINI_API_KEY", "GH_TOKEN", "DASHBOARD_PASSWORD"):
        if not env.get(k) and p14.get(k):
            env[k] = p14[k]
            save_env_value(k, env[k])
    if not env.get("COMPOSIO_API_KEY"):
        for k, v in composio_env().items():
            env[k] = v
            save_env_value(k, v)
    if not env.get("PIPELINE_SECRET"):
        env["PIPELINE_SECRET"] = secrets.token_urlsafe(32)
        save_env_value("PIPELINE_SECRET", env["PIPELINE_SECRET"])
    return env


def wrangler(*args: str, cwd: Path = WORKER, stdin: str | None = None, check: bool = True) -> str:
    res = subprocess.run(["npx", "wrangler", *args], cwd=cwd, input=stdin, text=True, capture_output=True)
    out = res.stdout + res.stderr
    if check and res.returncode != 0:
        raise SystemExit(f"wrangler {' '.join(args[:3])} failed:\n{out[-1500:]}")
    return out


def sql_value(v) -> str:
    return "'" + json.dumps(v).replace("'", "''") + "'"


def copy_instagram_tokens() -> None:
    """Reuse the Instagram reading connection of Naya News Autopilot (same Meta app and Page)."""
    if not (P14 / "worker" / "wrangler.toml").exists():
        return
    keys = ", ".join(f"'{k}'" for k in IG_KEYS)
    out = wrangler("d1", "execute", "naya-news-db", "--remote", "--json", "--command",
                   f"SELECT key, value FROM settings WHERE key IN ({keys})", cwd=P14 / "worker", check=False)
    try:
        rows = json.loads(out[out.index("["):])[0]["results"]
    except (ValueError, KeyError, IndexError):
        print("Instagram tokens not copied (Project14 database not readable)")
        return
    if not rows:
        return
    values = ", ".join(f"('{r['key']}', {sql_value(json.loads(r['value']))})" for r in rows)
    wrangler("d1", "execute", DB_NAME, "--remote", "--yes", "--command",
             f"INSERT INTO settings (key, value) VALUES {values} ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    print(f"Instagram reading copied from Naya News Autopilot ({len(rows)} values)")


def setup_worker(env: dict) -> str:
    if not (WORKER / "node_modules").exists():
        subprocess.run(["npm", "install", "--silent"], cwd=WORKER, check=True)
    toml_path = WORKER / "wrangler.toml"
    toml = toml_path.read_text()
    if "REPLACE_D1_ID" in toml:
        listing = json.loads(wrangler("d1", "list", "--json"))
        db = next((d for d in listing if d["name"] == DB_NAME), None)
        if not db:
            wrangler("d1", "create", DB_NAME)
            listing = json.loads(wrangler("d1", "list", "--json"))
            db = next(d for d in listing if d["name"] == DB_NAME)
        toml_path.write_text(toml.replace("REPLACE_D1_ID", db["uuid"]))
        print("D1 database ready")
    wrangler("d1", "execute", DB_NAME, "--remote", "--file", "schema.sql", "--yes")
    wrangler("d1", "execute", DB_NAME, "--remote", "--file", "seed.sql", "--yes")
    # Columns added after the first release; "duplicate column" just means it is already there.
    for stmt in ("ALTER TABLE topics ADD COLUMN keywords TEXT NOT NULL DEFAULT ''",):
        wrangler("d1", "execute", DB_NAME, "--remote", "--yes", "--command", stmt, check=False)
    if env.get("EMAIL_TO"):
        wrangler("d1", "execute", DB_NAME, "--remote", "--yes", "--command",
                 f"INSERT OR IGNORE INTO settings (key, value) VALUES ('email_recipients', {sql_value([env['EMAIL_TO']])})")
    print("Database tables and starter topics ready")
    copy_instagram_tokens()

    out = wrangler("deploy")
    m = re.search(r"https://[\w.-]+\.workers\.dev", out)
    if not m:
        raise SystemExit("Deploy finished but no workers.dev URL was printed:\n" + out[-1500:])
    url = m.group(0)
    payload = json.dumps({k: env[k] for k in WORKER_SECRETS if env.get(k)})
    wrangler("secret", "bulk", stdin=payload)
    wrangler("d1", "execute", DB_NAME, "--remote", "--yes", "--command",
             f"INSERT INTO settings (key, value) VALUES ('public_url', {sql_value(url)}) ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    print(f"Worker deployed and secrets set: {url}")
    return url


def gh(method: str, path: str, token: str, **kw) -> requests.Response:
    return requests.request(method, GH_API + path, timeout=30, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"}, **kw)


def seal(public_key_b64: str, value: str) -> str:
    from nacl import encoding, public
    box = public.SealedBox(public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder()))
    return base64.b64encode(box.encrypt(value.encode())).decode()


def setup_github(env: dict, worker_url: str | None) -> None:
    token = env["GH_TOKEN"]
    user = gh("GET", "/user", token)
    user.raise_for_status()
    owner = user.json()["login"]
    repo = f"{owner}/{REPO_NAME}"
    if gh("GET", f"/repos/{repo}", token).status_code == 404:
        # Public: GitHub Actions minutes are unlimited. No keys or news data live in the code.
        r = gh("POST", "/user/repos", token, json={
            "name": REPO_NAME, "private": False, "has_issues": False, "has_wiki": False,
            "description": "Free scheduled news digest: RSS, X and Instagram sorted by topic and emailed"})
        r.raise_for_status()
        print(f"Created repository {repo}")

    if not (ROOT / ".git").exists():
        raise SystemExit("Project folder is not its own git repository; run `git init` here first.")
    subprocess.run(["git", "add", "-A"], cwd=ROOT, check=True)
    subprocess.run(["git", "-c", "user.name=News Digest Autopilot", "-c", f"user.email={owner}@users.noreply.github.com",
                    "commit", "-q", "-m", "Update News Digest Autopilot"], cwd=ROOT)
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    push = subprocess.run(["git", "-c", f"http.extraHeader=Authorization: Basic {auth}", "push", "-q",
                           f"https://github.com/{repo}.git", "HEAD:main"], cwd=ROOT, capture_output=True, text=True)
    if push.returncode != 0:
        raise SystemExit("git push failed: " + push.stderr.replace(token, "***")[-800:])
    print("Code pushed to GitHub")

    key = gh("GET", f"/repos/{repo}/actions/secrets/public-key", token).json()
    for name in ("PIPELINE_SECRET", "GEMINI_API_KEY"):
        if env.get(name):
            r = gh("PUT", f"/repos/{repo}/actions/secrets/{name}", token,
                   json={"encrypted_value": seal(key["key"], env[name]), "key_id": key["key_id"]})
            r.raise_for_status()
    if worker_url:
        r = gh("PATCH", f"/repos/{repo}/actions/variables/WORKER_URL", token, json={"name": "WORKER_URL", "value": worker_url})
        if r.status_code == 404:
            gh("POST", f"/repos/{repo}/actions/variables", token, json={"name": "WORKER_URL", "value": worker_url}).raise_for_status()
    print("GitHub secrets and variables set")


def main() -> None:
    env = load_env()
    missing = [k for k in ("GEMINI_API_KEY", "GH_TOKEN", "DASHBOARD_PASSWORD") if not env.get(k)]
    if missing:
        raise SystemExit("Fill these in .env first: " + ", ".join(missing))
    worker_url = env.get("WORKER_URL")
    if "--skip-worker" not in sys.argv:
        worker_url = setup_worker(env)
        save_env_value("WORKER_URL", worker_url)
    if "--skip-github" not in sys.argv:
        setup_github(env, worker_url)
    print("Setup complete.")


if __name__ == "__main__":
    main()
