#!/bin/bash
# Setup Revolut Business API na VPS. Correr como root em /opt/omnai-stack.
#   ./agents/scripts/revolut_setup.sh cert            gera chave + certificado e imprime o que colar no Revolut
#   ./agents/scripts/revolut_setup.sh client <ID>     grava o ClientId que o Revolut mostra depois de carregar o cert
#   ./agents/scripts/revolut_setup.sh code <CODE>     troca o code do consentimento por tokens (2 minutos!)
#   ./agents/scripts/revolut_setup.sh test            testa a ligacao
set -eu
S=/opt/omnai-stack/secrets
CFG=$S/revolut.json
ISS=${ISS:-agents.omnai.pt}
case "${1:-}" in
  cert)
    umask 077
    openssl genrsa -traditional -out $S/revolut_private.pem 2048 2>/dev/null
    openssl req -new -x509 -key $S/revolut_private.pem -out $S/revolut_public.cer -days 1825 -subj "/C=PT/O=OMNAI/CN=$ISS"
    [ -f $CFG ] || printf '{"iss": "%s", "private_key_file": "revolut_private.pem", "env": "prod", "client_id": ""}\n' "$ISS" > $CFG
    echo "== Colar isto em Revolut Business > Definicoes > APIs > Business API > Add certificate:"
    cat $S/revolut_public.cer
    echo "== OAuth redirect URI: https://$ISS/revolut/callback"
    echo "== Depois: $0 client <ClientId>" ;;
  client)
    python3 - "$2" <<'EOF'
import json,sys; p="/opt/omnai-stack/secrets/revolut.json"; c=json.load(open(p)); c["client_id"]=sys.argv[1]; json.dump(c,open(p,"w"),indent=1); print("client_id gravado")
EOF
    echo "== Agora clica 'Enable API access' no Revolut, autoriza com 2FA, e copia o parametro code= do URL de redirect."
    echo "== Em 2 minutos: $0 code <CODE>" ;;
  code)
    docker exec omnai_agents python -c "from services import revolut; print(revolut.exchange_code('$2'))" ;;
  test)
    docker exec omnai_agents python -c "from services import revolut; import json; print(json.dumps(revolut.test_connection(), indent=1))" ;;
  *) echo "uso: $0 cert|client <id>|code <code>|test"; exit 1 ;;
esac
