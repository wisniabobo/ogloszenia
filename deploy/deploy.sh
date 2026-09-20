#!/usr/bin/env bash
# Aktualizacja działającej instalacji na serwerze.
#
#   ./deploy/deploy.sh root@bot.wisnia.dev
#
# Pierwszą instalację robi deploy/install.sh — ten skrypt zakłada, że usługi
# już istnieją, i tylko podciąga nowy kod.
#
# Logowanie wyłącznie po kluczu SSH. Skrypt nie przyjmuje ani nie zapisuje
# żadnych haseł; `BatchMode=yes` sprawi, że przy braku klucza po prostu
# odmówi, zamiast pytać o hasło.
#
# Serwis stoi na systemd i wirtualnym środowisku Pythona, nie na Dockerze.
# To był świadomy wybór: Docker przy starcie przestawia reguły iptables,
# a na tej maszynie działa kilkanaście innych witryn, którym nie wolno
# przerwać ruchu.
set -euo pipefail

TARGET="${1:-}"
REMOTE_DIR="${REMOTE_DIR:-/opt/ogloszenia}"
APP_USER="${APP_USER:-ogl}"
BRANCH="${BRANCH:-main}"

if [[ -z "$TARGET" ]]; then
	echo "użycie: $0 user@host   (np. $0 root@bot.wisnia.dev)" >&2
	exit 1
fi

echo "==> Aktualizuję $TARGET:$REMOTE_DIR (gałąź $BRANCH)"

ssh -o BatchMode=yes -o PasswordAuthentication=no "$TARGET" bash -euo pipefail <<REMOTE
	cd "$REMOTE_DIR"

	echo "==> Pobieram kod"
	sudo -u "$APP_USER" git fetch -q --all --prune
	sudo -u "$APP_USER" git reset -q --hard "origin/$BRANCH"
	echo "    wersja: \$(git rev-parse --short HEAD)"

	echo "==> Doinstalowuję zależności"
	sudo -u "$APP_USER" "$REMOTE_DIR/.venv/bin/pip" install -q -r requirements.txt

	echo "==> Domykam schemat bazy i wczytuję rejestr źródeł"
	sudo -u "$APP_USER" "$REMOTE_DIR/.venv/bin/python" -m ogloszenia.cli init-db

	echo "==> Restartuję interfejs"
	systemctl restart ogloszenia-web
	systemctl is-active ogloszenia-web

	echo "==> Stan zbierania"
	systemctl list-timers --all --no-legend | grep ogloszenia || true
REMOTE

echo "==> Gotowe. Sprawdź: https://bot.wisnia.dev/api/health"
