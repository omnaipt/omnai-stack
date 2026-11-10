# ADR 0001: Deploy do omnai-stack passa a ser git-based

- Estado: aceite (David, 10-11-2026), com duas condições abaixo garantidas
- Data: 2026-11-10
- Autor: Marco (Tech Lead)
- Contexto do trigger: BKL-157 (briefing Hoje)

## Condições de aceitação (David, 10-11-2026)

1. **Rollback para a tag anterior** tem de existir e estar documentado (ver "Rollback" abaixo).
2. **`.env` e `secrets/` ficam sempre fora do Git** e nunca são tocados pelo `git reset --hard` (ver "Segredos").

## Contexto

O serviço `omnai_agents` (FastAPI + workers, Docker, VPS Hostinger em
`/opt/omnai-stack`, servido em agents.omnai.pt) é hoje colocado em produção
por duas vias manuais:

1. **Tarball**: upload de `omnai-stack-final.tar.gz` via Cyberduck, extração
   para `/opt`, `docker compose build && up -d` (ver README "Deploy limpo").
2. **Edição direta no host**: `nano` num ficheiro, `docker restart` (ver o
   procedimento Notion "Procedimentos Dev: Deploy e Edição de Ficheiros em
   Docker VPS").

Nenhuma das vias parte do Git. O Git (`omnaipt/omnai-stack`) é um espelho
atualizado a posteriori por snapshots (ex.: branch `vps-snapshot-2026-07-07`,
commits "Espelho da producao"), o que o torna uma cópia atrasada e não a
fonte do que corre.

### Prova do problema (BKL-157, 10-11-2026)

Ao verificar md5 dos ficheiros em âmbito, container `/app` vs Git
`mcp-2026-09-08`: 8 de 9 batiam, mas `workers/email_scan.py` divergia. A VPS
corria uma versão de **16-10-2026 nunca commitada** (cache de classificação
em Redis + heurísticas a arquivar em vez de apagar). Edição por `nano` no
host, perdida para o Git. Qualquer deploy por tarball a partir do Git teria
esmagado essa melhoria viva, sem aviso.

Este é o modo de falha do deploy manual: silencioso, parece correto, e só se
descobre por auditoria. Com 1 pessoa e uma equipa de agentes a mexer, não é
sustentável.

## Decisão

O deploy do omnai-stack passa a ser **git-based**. A VPS deixa de ser editada
à mão; passa a ser um checkout de uma branch de produção, e o deploy é um
`git pull` seguido de rebuild e restart.

Mecânica proposta:

1. Uma única branch de produção (ex.: `mcp-2026-09-08`, a renomear para
   `production` numa limpeza futura). O `main` e as `feat/*` ficam para
   trabalho; produção segue a branch de produção.
2. `/opt/omnai-stack` passa a ser um clone Git dessa branch (o `.env` e
   `secrets/` continuam fora do Git, gitignored, geridos pelo David no host).
3. Cada deploy cria uma **tag** `deploy-YYYYMMDD-HHMM` no commit que entra, para
   rollback determinístico.
4. Deploy = script `scripts/deploy.sh` no host:
   ```bash
   cd /opt/omnai-stack
   git fetch --all --tags
   PREV=$(git rev-parse HEAD)                   # guarda o anterior
   git reset --hard origin/<branch-producao>    # host nunca tem edicoes locais
   git tag "deploy-$(date +%Y%m%d-%H%M)"
   echo "$PREV" > .last_deploy                   # para o rollback
   docker compose up -d --build
   docker compose logs --tail=15 agents-api
   ```
5. Proibido `nano`/`docker cp` no host para alterar código. Qualquer fix passa
   por PR → merge na branch de produção → `deploy.sh`.
6. Opcional fase 2: GitHub Action que faz SSH + `deploy.sh` no merge para a
   branch de produção (CI-on-merge, como já existe no Palaestra).

### Rollback

```bash
cd /opt/omnai-stack
git reset --hard $(cat .last_deploy)      # ou: git reset --hard <tag anterior>
docker compose up -d --build
```
`git tag -l 'deploy-*'` lista os pontos de rollback. Como `.env`/`secrets` estão
fora do Git, o rollback só mexe em código; a configuração mantém-se.

### Segredos (condição 2)

`.env` e `secrets/` estão no `.gitignore` e vivem só no host (`/opt/omnai-stack`),
geridos pelo David. O `git reset --hard` só reescreve ficheiros versionados, por
isso **nunca** toca em `.env`/`secrets/`. A chave de serviço Supabase e tokens
OAuth nunca entram no repo. Antes da migração, confirmar com
`git check-ignore .env secrets/` que ambos estão ignorados.

## Consequências

Positivas:
- Git volta a ser a fonte de verdade; fim do drift silencioso.
- Deploy reproduzível e auditável (cada deploy = um commit conhecido).
- Rollback trivial: `git reset --hard <commit anterior>` + rebuild.

Custos / riscos:
- `git reset --hard` no host apaga edições locais não commitadas. É o
  objetivo (força a disciplina), mas exige que o `.env`/`secrets` estejam
  mesmo fora do Git (já estão).
- Uma migração única: transformar `/opt/omnai-stack` (extraído de tarball) num
  clone Git sem perder o estado atual. Fazer depois de garantir que a branch
  de produção == container (md5 9/9), para o primeiro `reset --hard` ser
  no-op.

## Relação com o BKL-157

Ramificar e desenvolver o BKL-157 **não depende** deste ADR. Mas o **deploy**
do BKL-157 para produção **depende**: não vai por tarball nem por `nano`. Ou
este ADR é aceite e a migração git-based feita primeiro, ou o deploy do
BKL-157 fica bloqueado à espera dela. Decisão e timing com o David.
