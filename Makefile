.PHONY: help up down logs restart build ps env-check tasks import-n8n

help:
	@echo "OMNAI Stack - comandos disponiveis"
	@echo "  make env-check    verifica .env"
	@echo "  make build        constroi imagens"
	@echo "  make up           levanta o stack em background"
	@echo "  make down         desliga tudo"
	@echo "  make restart      reinicia todos os servicos"
	@echo "  make logs         segue os logs"
	@echo "  make ps           lista contentores"
	@echo "  make tasks        mostra tasks carregadas pelo agents-api"
	@echo "  make install-cron instala crontab fallback em /etc/cron.d/omnai"

env-check:
	@test -f .env || (echo ">>> Cria o .env a partir de .env.example"; exit 1)
	@echo ".env OK"

build:
	docker compose build

up: env-check
	docker compose up -d

down:
	docker compose down

restart:
	docker compose restart

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

tasks:
	@curl -fsS http://localhost:8000/tasks 2>/dev/null | python3 -m json.tool || \
		docker compose exec -T agents-api curl -fsS http://localhost:8000/tasks | python3 -m json.tool

install-cron:
	sudo cp schedules/crontab.omnai /etc/cron.d/omnai
	sudo chmod 644 /etc/cron.d/omnai
	@echo "Cron instalado. Ver logs em /var/log/omnai-cron.log"
