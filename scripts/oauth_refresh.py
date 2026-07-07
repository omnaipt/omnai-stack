"""Refresh OAuth token Gmail. Funciona dentro do container ou no host."""
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from google_auth_oauthlib.flow import Flow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
]

# Auto-detect secrets path (container=/secrets, host=/opt/omnai-stack/secrets)
for candidate in (Path("/secrets"), Path("/opt/omnai-stack/secrets")):
    if (candidate / "credentials.json").exists():
        SECRETS = candidate
        break
else:
    print("ERRO: credentials.json nao encontrado em /secrets nem /opt/omnai-stack/secrets")
    sys.exit(1)

CRED = SECRETS / "credentials.json"
TOKENS_DIR = SECRETS / "tokens"
print(f"Usando SECRETS={SECRETS}")

if len(sys.argv) < 2:
    print("uso: oauth_refresh.py <email>")
    sys.exit(1)

account = sys.argv[1]
stem = account.replace("@", "_at_").replace(".", "_")
out = TOKENS_DIR / f"{stem}.json"

flow = Flow.from_client_secrets_file(
    str(CRED), scopes=SCOPES, redirect_uri="http://localhost"
)
url, _ = flow.authorization_url(
    access_type="offline", include_granted_scopes="true",
    prompt="consent", login_hint=account,
)
print("\n1. Abre esta URL no browser do teu PC:\n")
print(url)
print("\n2. Faz login com:", account)
print("3. Autoriza.")
print("4. Browser vai falhar (http://localhost). Copia a URL completa da barra.")
print()
response = input("Cola URL: ").strip()
parsed = urlparse(response)
code = (parse_qs(parsed.query).get("code") or [None])[0]
if not code:
    print("ERRO: 'code' nao encontrado na URL")
    sys.exit(2)

flow.fetch_token(code=code)
if out.exists():
    out.replace(out.with_suffix(".json.bak"))
out.write_text(flow.credentials.to_json(), encoding="utf-8")
print(f"OK token guardado: {out}")
