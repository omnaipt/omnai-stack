#!/usr/bin/env bash
#
# OMNAI VPS Bootstrap (Ubuntu 22.04 LTS)
# Corre como root, uma unica vez, logo depois de ligar ao VPS por SSH.
#
# O que faz:
#   1. Actualiza o sistema
#   2. Instala pacotes base (curl, gnupg, ufw, fail2ban, git, etc.)
#   3. Instala Docker Engine + plugin Compose
#   4. Cria utilizador "omnai" com sudo sem password e grupo docker
#   5. Copia a chave SSH do root para o omnai (para poderes entrar como omnai)
#   6. Activa firewall (22, 80, 443) e fail2ban
#   7. Activa unattended-upgrades (patches de seguranca automaticos)
#   8. Prepara /opt/omnai-stack
#
# Depois deste script:
#   exit  # sai do root
#   ssh -i ~/.ssh/hostinger_vps_ed25519 omnai@IP_DO_VPS
#   cd /opt/omnai-stack && cp .env.example .env && nano .env
#   docker compose up -d

set -euo pipefail

log() { printf '\n\033[1;36m[bootstrap] %s\033[0m\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Tem de correr como root. Use: sudo bash bootstrap.sh"
  exit 1
fi

log "1/8 Actualizar pacotes do sistema"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y

log "2/8 Instalar pacotes base"
apt-get install -y \
  ca-certificates curl gnupg lsb-release \
  ufw fail2ban unattended-upgrades \
  git htop vim jq rsync tmux \
  python3 python3-pip python3-venv

log "3/8 Instalar Docker Engine + Compose plugin"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg

  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list

  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io \
                     docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

log "4/8 Criar utilizador omnai"
if ! id -u omnai >/dev/null 2>&1; then
  useradd -m -s /bin/bash omnai
  usermod -aG sudo,docker omnai
  install -d -m 700 -o omnai -g omnai /home/omnai/.ssh
  if [[ -f /root/.ssh/authorized_keys ]]; then
    cp /root/.ssh/authorized_keys /home/omnai/.ssh/authorized_keys
    chown omnai:omnai /home/omnai/.ssh/authorized_keys
    chmod 600 /home/omnai/.ssh/authorized_keys
  fi
  echo "omnai ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/omnai
  chmod 440 /etc/sudoers.d/omnai
fi

log "5/8 Configurar firewall UFW"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

log "6/8 Activar fail2ban"
systemctl enable --now fail2ban

log "7/8 Activar unattended-upgrades"
dpkg-reconfigure -f noninteractive unattended-upgrades || true
systemctl enable --now unattended-upgrades

log "8/8 Criar /opt/omnai-stack"
install -d -m 775 -o omnai -g docker /opt/omnai-stack

cat <<EOF

=========================================================
Bootstrap concluido.

Proximos passos:
  1) Sai do root:            exit
  2) Entra como omnai:       ssh -i ~/.ssh/hostinger_vps_ed25519 omnai@IP_DO_VPS
  3) Copia o stack:           via Cyberduck para /opt/omnai-stack/
                              (ou: scp -r * omnai@IP:/opt/omnai-stack/)
  4) cd /opt/omnai-stack
  5) cp .env.example .env && nano .env   (preencher segredos)
  6) docker compose build
  7) docker compose up -d
  8) docker compose logs -f

Docker version: $(docker --version)
Compose version: $(docker compose version --short)
=========================================================
EOF
