# OMNAI Stack

Stack self-hosted que suporta os agentes virtuais da OMNAI (Ana, Marco, Sofia, Zé, Rita, Tiago, Beatriz, Carlos). Corre num VPS Hostinger (Ubuntu 24.04) com Traefik e n8n já fornecidos pela Hostinger.

Este pacote é a versão final consolidada (v1 → v5) pronta para instalação limpa.

## Capacidades já implementadas

| Worker | Faz |
|--------|-----|
| `briefing-carlos` | Compoe briefing matinal em Notion: carry-over de to-dos não marcados, prioridades em checkboxes, tabela de caixas de correio, secção de drafts de resposta |
| `email-scan` | Lê 3 Gmail (OAuth) + 3 IMAP (SAPO/Hostinger), classifica (conservador via Claude), cria drafts em Gmail para respostas, guarda stats e drafts em Redis |

Os outros 12 workers ainda devolvem stub "accepted" até serem implementados.

## Arquitectura

```
       DNS agents.omnai.pt -> 187.124.42.68
                   |
              Traefik (Hostinger) :80/:443
                   |
                   v
          127.0.0.1:8010 -> agents-api :8000 (FastAPI)
                   |
       +-----------+-----------+--------+
       v           v           v        v
   postgres     redis      Gmail API    IMAP (Hostinger/SAPO)
   (stats)    (drafts)   (3 contas)     (3 contas)
                                |
                                v
                          Anthropic API
                          Notion API
```

Routing Traefik configurado em `/etc/traefik/dynamic.yml`:

```yaml
http:
  routers:
    omnai-agents:
      rule: "Host(`agents.omnai.pt`)"
      entryPoints: [websecure]
      service: omnai-agents
      tls:
        certResolver: letsencrypt
  services:
    omnai-agents:
      loadBalancer:
        servers:
          - url: "http://localhost:8010"
```

## Estrutura de ficheiros

```
omnai-stack/
  bootstrap.sh              # Prep do VPS (Docker, UFW, user omnai) - so primeira vez
  docker-compose.yml        # 3 servicos: postgres, redis, agents-api
  Makefile                  # atalhos (make up, make logs, etc.)
  .env.example              # template
  .gitignore
  README.md
  agents/
    Dockerfile
    requirements.txt        # FastAPI, Anthropic, Google Gmail, Notion, etc.
    main.py                 # Dispatcher FastAPI
    auth.py                 # Middleware X-OMNAI-Token
    services/
      llm.py                # Wrapper Anthropic
      notion.py             # Cliente Notion API
      state.py              # Redis shared state
      gmail.py              # Gmail OAuth
      imap_client.py        # IMAP Hostinger/SAPO
      classifier.py         # Claude classifier conservador
      drafter.py            # Claude drafter de respostas
    utils/
      notion_blocks.py      # Helpers blocos Notion (tabelas, bookmarks, etc.)
    workers/
      briefing_carlos.py    # Worker real
      email_scan.py         # Worker real
  schedules/
    schedules.json          # 14 tasks migradas do Cowork
    crontab.omnai           # Fallback host cron
    n8n_template.json       # Base dos workflows
    n8n_workflows/          # 14 workflows prontos a importar
  scripts/
    regenerate_workflows.py # Re-cria workflows com token novo
  secrets/                  # Credenciais (gitignored) - ver secrets/README.md
```

## Deploy limpo (primeira vez no VPS)

Pressupoe que ja fizeste Fase 0 (bootstrap do VPS, Docker/Compose instalados, Traefik a correr, integracao Notion conectada ao Command Center, chave Anthropic valida, DNS agents.omnai.pt a apontar).

```bash
# 1. Upload do tarball via Cyberduck para /root/

# 2. Extrair para /opt
cd /opt
# Se ja existe, fazer backup primeiro
[ -d omnai-stack ] && mv omnai-stack omnai-stack.bak.$(date +%Y%m%d-%H%M)
tar -xzf /root/omnai-stack-final.tar.gz
mv omnai-stack-final omnai-stack
cd /opt/omnai-stack
chmod +x bootstrap.sh scripts/regenerate_workflows.py

# 3. Configurar .env
cp .env.example .env
# Gerar segredos
POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)
REDIS_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)
OMNAI_API_TOKEN=$(openssl rand -hex 32)
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${POSTGRES_PASSWORD}|" .env
sed -i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=${REDIS_PASSWORD}|" .env
sed -i "s|^OMNAI_API_TOKEN=.*|OMNAI_API_TOKEN=${OMNAI_API_TOKEN}|" .env
# Editar manualmente ANTHROPIC_API_KEY e NOTION_TOKEN
nano .env

# 4. Upload dos secrets via Cyberduck para /opt/omnai-stack/secrets/
#    (ver secrets/README.md para a estrutura esperada)
mkdir -p secrets/tokens
chmod 700 secrets secrets/tokens
# apos upload:
chmod 600 secrets/*.json secrets/tokens/*.json 2>/dev/null || true

# 5. Rota Traefik (se ainda nao existe)
grep -q "agents.omnai.pt" /etc/traefik/dynamic.yml || python3 <<'PY'
import pathlib
p = pathlib.Path("/etc/traefik/dynamic.yml")
c = p.read_text()
router = '''    omnai-agents:
      rule: "Host(`agents.omnai.pt`)"
      entryPoints:
        - websecure
      service: omnai-agents
      tls:
        certResolver: letsencrypt
'''
service = '''    omnai-agents:
      loadBalancer:
        servers:
          - url: "http://localhost:8010"
'''
lines = c.splitlines()
out = []
inserted = False
for ln in lines:
    if ln.strip() == "services:" and not inserted:
        out.append(router.rstrip("\n"))
        inserted = True
    out.append(ln)
out.append(service.rstrip("\n"))
p.write_text("\n".join(out) + "\n")
print("rota agents.omnai.pt adicionada")
PY
docker restart traefik

# 6. Regenerar workflows n8n com o novo token
python3 scripts/regenerate_workflows.py \
  --schedules schedules/schedules.json \
  --out schedules/n8n_workflows \
  --token "${OMNAI_API_TOKEN}"

# 7. Build e arrancar
docker compose build
docker compose up -d
sleep 8
docker compose logs --tail=15 agents-api

# 8. Testes basicos
echo "=== /health ==="
curl -sS https://agents.omnai.pt/health | python3 -m json.tool

echo "=== /tasks com token ==="
curl -sS -H "X-OMNAI-Token: ${OMNAI_API_TOKEN}" https://agents.omnai.pt/tasks \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('tasks:', d['count'])"

echo "=== credenciais email ==="
docker exec omnai_agents python3 -c "
from services import gmail, imap_client
for acc in gmail.GMAIL_ACCOUNTS:
    print(f'Gmail {acc}: {gmail.test_connection(acc)}')
for acc in imap_client.available_accounts():
    print(f'IMAP  {acc}: {imap_client.test_connection(acc)}')
"

# 9. Primeiro scan real (2-5 min)
curl -sS --max-time 600 -X POST -H "X-OMNAI-Token: ${OMNAI_API_TOKEN}" \
  https://agents.omnai.pt/tasks/run/email-scan | python3 -m json.tool

# 10. Briefing com dados reais
curl -sS --max-time 180 -X POST -H "X-OMNAI-Token: ${OMNAI_API_TOKEN}" \
  https://agents.omnai.pt/tasks/run/briefing-carlos | python3 -m json.tool

# 11. Importar workflows no n8n
for wf in schedules/n8n_workflows/*.json; do
  docker exec -i n8n-gbn2-n8n-1 n8n import:workflow --input=/dev/stdin < "$wf"
done
for id in $(docker exec n8n-gbn2-n8n-1 n8n list:workflow | grep '|OMNAI' | cut -d'|' -f1); do
  docker exec n8n-gbn2-n8n-1 n8n publish:workflow --id="$id" 2>&1 | tail -1
done
docker restart n8n-gbn2-n8n-1
```

## Workflow tipico apos deploy

**Todos os dias uteis as 07:54 UTC**: n8n dispara `email-scan` que le as 8 caixas, classifica, cria drafts no Gmail.

**As 08:18 UTC**: n8n dispara `briefing-carlos` que compoe o briefing em Notion com a tabela de caixas, drafts recentes, e carry-over dos to-dos nao marcados.

**Tu abres a pagina Morning Briefing no Notion** ao inicio do dia. Vês:
- O que ficou por fazer de ontem (carry-over)
- Prioridades hoje (checkboxes)
- Alertas e deadlines
- Tabela com contadores por caixa
- Rascunhos de resposta com links para o Gmail Drafts

## Seguranca

- `.env` gitignored, tokens OAuth em `secrets/` gitignored
- `agents-api` nao exposto directamente, so via Traefik com cert Let's Encrypt
- Todos os endpoints excepto `/health` exigem `X-OMNAI-Token`
- UFW: 22, 80, 443 apenas
- fail2ban activo para brute-force SSH

## Comandos uteis

```bash
make up          # levantar stack
make down        # desligar
make logs        # seguir logs
make ps          # estado dos contentores
make tasks       # listar tasks carregadas

# Ver drafts pendentes
docker exec omnai_redis redis-cli -a $REDIS_PASSWORD LRANGE omnai:email:drafts 0 -1

# Ver stats de email por conta
docker exec omnai_redis redis-cli -a $REDIS_PASSWORD KEYS 'omnai:email:stats:*'
docker exec omnai_redis redis-cli -a $REDIS_PASSWORD HGETALL 'omnai:email:stats:davidsardinhalves@gmail.com'
```

## Proximos passos (roadmap)

| Fase | Objectivo |
|------|-----------|
| 3 | Respeitar rate limits Gmail + label "OMNAI-Processed" para evitar re-processar |
| 3 | Workers reais para `scan-concursos-publicos`, `jmsoares-concursos-seguranca`, `pipeline-review-semanal` |
| 4 | Workers da Ana (fecho contabilistico, recibos Sopato, arquivo faturas) |
| 4 | Integracao Google Drive (faturas) |
| 5 | Observabilidade (Grafana, Loki ou Uptime Kuma) |
| 5 | Backup automatico Postgres para B2/R2 off-site |
