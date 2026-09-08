# MCP remoto da stack OMNAI (08-09-2026)

Servidor MCP (Streamable HTTP, JSON-RPC) dentro da API FastAPI da VPS Hostinger,
em `https://agents.omnai.pt/mcp`. Dá ao Claude (claude.ai, Cowork, Claude Code)
acesso às 4 caixas Gmail do David, ao índice de faturas, à fila Hoje, às tarefas
e aos dados das empresas, reutilizando os tokens OAuth que a VPS já tem.

## Ficheiros

| Ficheiro | Papel |
|---|---|
| `deploy/mcp_server.py` | Servidor MCP. 16 tools. Sem dependências novas (o SDK `mcp` não está na imagem e o código é bind-mounted). |
| `deploy/pwa_gate.py` | Portão de sessão da PWA. Só activo com `/opt/omnai-stack/secrets/pwa_gate.json`. |
| `deploy/main_py_patch.txt` | 6 linhas acrescentadas ao fim de `main.py` (já aplicadas em produção). |
| `deploy/deploy.sh` | Deploy idempotente via git-bash + ssh: backup, patch, token, verificação de sintaxe e imports, restart, smoke test. |
| `deploy/deploy_log.txt` | Log do deploy de 08-09 (contém o token no fim; apagar depois de guardar no Bitwarden). |
| `deploy/mcp_token.txt` | Token do MCP. Guardar no Bitwarden (colecção Infraestrutura) e apagar este ficheiro. |
| `tests/test_mcp.py` | 7 testes (auth, initialize, tools/list, search/thread/attachment, erros, batch, portão). Correm com `pytest` e stubs dos services. |
| `vps_snapshot/` | Cópia do código vivo da VPS em 08-09 (main.py, services relevantes, compose). A produção diverge do repo `omnaipt/omnai-stack` desde Maio. |
| `anexos_para_eugest/` | PDFs MEO 26/07 e Supabase 17/07, copiados do arquivo da VPS para anexar à resposta. |
| `draft_resposta_eugest.txt` | Texto do rascunho criado no Gmail (david.sardinha@omnai.pt, thread de 1/09). |
| `email_eugest_*.png` | As tabelas que vinham como imagens inline no email de 1/09. |

## Ligar ao Claude

Definições > Conectores > Adicionar conector personalizado:

`https://agents.omnai.pt/mcp/<token>`

O token no caminho existe porque o formulário do claude.ai não aceita headers.
Claude Code e `mcp-remote` podem usar `Authorization: Bearer <token>` ou
`X-OMNAI-Token: <token>` em `https://agents.omnai.pt/mcp`.

## Tools

`list_accounts`, `search_mail` (sintaxe Gmail, conta ou `all`), `get_thread`,
`get_message`, `read_attachment` (PDF e texto extraídos; base64 opcional),
`create_draft` (rascunho, nunca envia), `email_inbox_pending`,
`faturas_resumo`, `list_faturas`, `read_fatura`, `fatura_accao`,
`faturas_pacote`, `list_hoje`, `list_tarefas`, `criar_tarefa`, `empresas_dados`.

Escritas: só `create_draft`, `criar_tarefa` e `fatura_accao`.

## Rodar o token

```
ssh root@187.124.42.68 'openssl rand -hex 32 > /opt/omnai-stack/secrets/mcp_token.txt'
```

Sem restart: o ficheiro é lido a cada pedido.

## Ligar o portão da PWA

```
ssh root@187.124.42.68 'printf "{\"frase\": \"FRASE\", \"chave\": \"%s\"}\n" $(openssl rand -hex 32) > /opt/omnai-stack/secrets/pwa_gate.json && chmod 600 /opt/omnai-stack/secrets/pwa_gate.json'
```

Sem restart. A partir daí a app pede a frase uma vez por dispositivo (cookie 90 dias).
Isentos: `/health`, `/actions/*`, `/api/telegram/*`, `/mcp*`, `/manifest.json`,
`/sw.js`, `/static/*`, `/favicon.ico`, `/instalar`. Para desligar: apagar o ficheiro.

## Rollback do MCP

```
ssh root@187.124.42.68 'cd /opt/omnai-stack/agents && cp main.py.bak-mcp-20260908 main.py && docker restart omnai_agents'
```

## Limitações conhecidas

- Sem SSE: GET /mcp devolve 405. Os clientes actuais funcionam só com POST.
- `create_draft` só aceita endereços simples no `to` (sem nomes com acentos) e não tem Cc; a Inês foi metida no `to`.
- `search_mail` com `format=metadata` não lista anexos; `get_thread`/`get_message` listam.
- sapo.pt (IMAP) e previnsa.com (reencaminhada para opaidapetinga) não têm entrada própria.
- O token no URL fica nos logs do Traefik/uvicorn. Rodar periodicamente.
