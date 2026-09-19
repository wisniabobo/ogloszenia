#!/usr/bin/env bash
#
# Instalacja na świeżym serwerze — uruchamiana BEZPOŚREDNIO NA NIM.
#
#   curl -fsSL https://raw.githubusercontent.com/wisniabobo/ogloszenia/main/deploy/bootstrap.sh \
#     | sudo bash -s -- bot.wisnia.dev twoj@email.pl
#
# Co robi:
#   1. instaluje Dockera, jeśli go nie ma,
#   2. klonuje repozytorium do /opt/ogloszenia,
#   3. tworzy .env z losową solą do haszowania numerów,
#   4. uruchamia aplikację na 127.0.0.1:8000 (nie zajmuje portów 80/443),
#   5. jeśli na serwerze jest nginx — dokłada vhosta dla podanej domeny
#      i zostawia resztę konfiguracji nietkniętą,
#   6. jeśli jest certbot — wystawia certyfikat dla tej jednej domeny,
#   7. robi pierwszy skan i geokodowanie.
#
# Skrypt jest idempotentny: można go uruchomić ponownie, żeby zaktualizować.
#
set -euo pipefail

DOMAIN="${1:-}"
ACME_EMAIL="${2:-}"
REPO="${REPO:-https://github.com/wisniabobo/ogloszenia.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/ogloszenia}"

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

[[ -n "$DOMAIN" ]] || die "użycie: bootstrap.sh <domena> [email-do-certyfikatu]"
[[ $EUID -eq 0 ]] || die "uruchom przez sudo"

# --------------------------------------------------------------------------- #
log "Sprawdzam, czy domena wskazuje na ten serwer"
SERVER_IP="$(curl -fsS --max-time 10 https://api.ipify.org || echo '')"
DOMAIN_IP="$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1 || echo '')"
if [[ -n "$SERVER_IP" && -n "$DOMAIN_IP" && "$SERVER_IP" != "$DOMAIN_IP" ]]; then
	warn "$DOMAIN wskazuje na $DOMAIN_IP, a serwer ma $SERVER_IP."
	warn "Certyfikat się nie wystawi, dopóki DNS nie będzie zgodny. Idę dalej."
fi

# --------------------------------------------------------------------------- #
log "Docker"
if ! command -v docker >/dev/null 2>&1; then
	curl -fsSL https://get.docker.com | sh
else
	echo "już zainstalowany: $(docker --version)"
fi
docker compose version >/dev/null 2>&1 || die "brakuje wtyczki docker compose"

# --------------------------------------------------------------------------- #
log "Kod źródłowy w $APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then
	git -C "$APP_DIR" fetch --all --prune
	git -C "$APP_DIR" reset --hard "origin/$BRANCH"
else
	command -v git >/dev/null || { apt-get update -qq && apt-get install -y -qq git; }
	mkdir -p "$(dirname "$APP_DIR")"
	git clone --branch "$BRANCH" --depth 20 "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"

# --------------------------------------------------------------------------- #
log "Konfiguracja (.env)"
if [[ ! -f .env ]]; then
	cp .env.example .env
	SALT="$(head -c 32 /dev/urandom | base64 | tr -d '\n/+=' | head -c 40)"
	{
		echo ""
		echo "# --- ustawione automatycznie przez bootstrap.sh ---"
		echo "DOMAIN=$DOMAIN"
		[[ -n "$ACME_EMAIL" ]] && echo "ACME_EMAIL=$ACME_EMAIL"
		echo "OGL_PHONE_HASH_SALT=$SALT"
		echo "OGL_DATABASE_URL=sqlite:////data/ogloszenia.db"
	} >> .env
	echo "utworzono .env (sól do haszowania numerów wylosowana)"
else
	echo ".env już istnieje — nie ruszam"
fi

# --------------------------------------------------------------------------- #
log "Buduję obraz i uruchamiam usługi"
docker compose up -d --build
sleep 5
docker compose exec -T web python -m ogloszenia.cli init-db || true

log "Sprawdzam, czy aplikacja odpowiada"
for _ in $(seq 1 30); do
	if curl -fsS --max-time 5 http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
		curl -fsS http://127.0.0.1:8000/api/health; echo; break
	fi
	sleep 2
done

# --------------------------------------------------------------------------- #
if command -v nginx >/dev/null 2>&1; then
	log "Wykryto nginx — dokładam vhosta dla $DOMAIN"
	VHOST="/etc/nginx/sites-available/$DOMAIN"

	if [[ -e "$VHOST" ]]; then
		cp -a "$VHOST" "$VHOST.bak.$(date +%s)"
		echo "istniejący vhost zachowany jako kopia .bak"
	fi
	sed "s/bot\.wisnia\.dev/$DOMAIN/g" deploy/nginx-vhost.conf > "$VHOST"
	ln -sf "$VHOST" "/etc/nginx/sites-enabled/$DOMAIN"
	mkdir -p /var/cache/nginx/ogloszenia
	chown -R www-data:www-data /var/cache/nginx/ogloszenia 2>/dev/null || true

	if nginx -t 2>/dev/null; then
		systemctl reload nginx
		echo "nginx przeładowany, pozostałe domeny bez zmian"
	else
		warn "konfiguracja nginxa nie przechodzi testu — cofam zmianę"
		rm -f "/etc/nginx/sites-enabled/$DOMAIN"
		nginx -t && systemctl reload nginx || true
	fi

	if [[ -n "$ACME_EMAIL" ]]; then
		log "Certyfikat Let's Encrypt"
		command -v certbot >/dev/null 2>&1 || {
			apt-get update -qq
			apt-get install -y -qq certbot python3-certbot-nginx
		}
		certbot --nginx -d "$DOMAIN" \
			--non-interactive --agree-tos -m "$ACME_EMAIL" --redirect \
			|| warn "certbot nie dał rady — sprawdź DNS i uruchom ręcznie: certbot --nginx -d $DOMAIN"
	else
		warn "bez adresu e-mail nie wystawiam certyfikatu."
		warn "uruchom potem: certbot --nginx -d $DOMAIN"
	fi
else
	warn "nginx nie znaleziony."
	warn "Albo zainstaluj nginxa i użyj deploy/nginx-vhost.conf,"
	warn "albo uruchom wbudowane Caddy: docker compose --profile caddy up -d"
fi

# --------------------------------------------------------------------------- #
log "Pierwszy skan (to potrwa kilka minut)"
docker compose exec -T web python -m ogloszenia.cli scan --pages 3 || true
docker compose exec -T web python -m ogloszenia.cli geocode --limit 1000 || true
docker compose exec -T web python -m ogloszenia.cli stats || true

log "Gotowe"
echo "  strona:     https://$DOMAIN"
echo "  API:        https://$DOMAIN/api/health"
echo "  dokumenty:  https://$DOMAIN/docs"
echo
echo "  logi zbierania:  cd $APP_DIR && docker compose logs -f worker"
echo "  aktualizacja:    sudo bash $APP_DIR/deploy/bootstrap.sh $DOMAIN ${ACME_EMAIL:-}"
