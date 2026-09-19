#!/usr/bin/env bash
# Wdrożenie na VPS. Uruchamiasz TY, ze swojego terminala, po kluczu SSH.
#
#   ./deploy/deploy.sh user@bot.wisnia.dev
#
# Skrypt nie przyjmuje i nie przechowuje żadnych haseł — logowanie po haśle
# jest wyłączone celowo. Jeśli nie masz jeszcze klucza na serwerze:
#
#   ssh-keygen -t ed25519 -C "deploy ogloszenia"
#   ssh-copy-id user@IP
#
set -euo pipefail

TARGET="${1:-}"
REMOTE_DIR="${REMOTE_DIR:-/opt/ogloszenia}"
BRANCH="${BRANCH:-main}"
REPO="${REPO:-https://github.com/wisniabobo/ogloszenia.git}"

if [[ -z "$TARGET" ]]; then
	echo "użycie: $0 user@host   (np. $0 root@bot.wisnia.dev)" >&2
	exit 1
fi

echo "==> Wdrażam $REPO ($BRANCH) na $TARGET:$REMOTE_DIR"

ssh -o BatchMode=yes -o PasswordAuthentication=no "$TARGET" bash -euo pipefail <<REMOTE
	if ! command -v docker >/dev/null; then
		echo "==> Instaluję Dockera"
		curl -fsSL https://get.docker.com | sh
	fi

	if [[ ! -d "$REMOTE_DIR/.git" ]]; then
		echo "==> Klonuję repozytorium"
		mkdir -p "$REMOTE_DIR"
		git clone --branch "$BRANCH" "$REPO" "$REMOTE_DIR"
	else
		echo "==> Aktualizuję repozytorium"
		git -C "$REMOTE_DIR" fetch --all --prune
		git -C "$REMOTE_DIR" reset --hard "origin/$BRANCH"
	fi

	cd "$REMOTE_DIR"

	if [[ ! -f .env ]]; then
		echo "==> Tworzę .env z szablonu — UZUPEŁNIJ DOMENĘ I E-MAIL"
		cp .env.example .env
		{
			echo ""
			echo "DOMAIN=bot.wisnia.dev"
			echo "ACME_EMAIL=zmien@na.swoj.email"
			echo "OGL_PHONE_HASH_SALT=\$(head -c 24 /dev/urandom | base64)"
		} >> .env
	fi

	echo "==> Buduję i uruchamiam"
	docker compose pull --ignore-buildable || true
	docker compose up -d --build

	echo "==> Inicjuję bazę"
	docker compose exec -T web python -m ogloszenia.cli init-db

	echo "==> Stan usług"
	docker compose ps
REMOTE

echo "==> Gotowe. Sprawdź: https://\${DOMAIN:-bot.wisnia.dev}/api/health"
