#!/usr/bin/env bash
#
# Instalacja bez Dockera — dla serwerów, na których już coś działa.
#
#   sudo bash deploy/install.sh bot.wisnia.dev twoj@email.pl
#
# Dlaczego nie Docker: jego demon przepisuje reguły iptables, a na serwerze
# z kilkunastoma działającymi witrynami to niepotrzebne ryzyko. Tutaj jest
# tylko wirtualne środowisko Pythona, usługa systemd słuchająca na 127.0.0.1
# i jeden nowy vhost nginxa. Nic poza tym nie jest ruszane.
#
# Skrypt jest idempotentny — uruchom ponownie, żeby zaktualizować.
#
set -euo pipefail

DOMAIN="${1:-}"
ACME_EMAIL="${2:-}"
REPO="${REPO:-https://github.com/wisniabobo/ogloszenia.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/metruj}"
APP_USER="${APP_USER:-metruj}"
PORT="${PORT:-8000}"

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

[[ -n "$DOMAIN" ]] || die "użycie: install.sh <domena> [email-do-certyfikatu]"
[[ $EUID -eq 0 ]] || die "uruchom przez sudo"

# --------------------------------------------------------------------------- #
log "Pakiety systemowe"
export DEBIAN_FRONTEND=noninteractive
MISSING=()
for pkg in git python3-venv python3-pip; do
	dpkg -s "$pkg" >/dev/null 2>&1 || MISSING+=("$pkg")
done
if ((${#MISSING[@]})); then
	echo "doinstaluję: ${MISSING[*]}"
	apt-get update -qq
	apt-get install -y -qq "${MISSING[@]}"
else
	echo "wszystko już jest"
fi

PYTHON="$(command -v python3.12 || command -v python3.11 || command -v python3)"
echo "Python: $PYTHON ($($PYTHON --version))"

# --------------------------------------------------------------------------- #
log "Użytkownik $APP_USER"
if ! id "$APP_USER" >/dev/null 2>&1; then
	useradd --system --create-home --home-dir "/home/$APP_USER" --shell /usr/sbin/nologin "$APP_USER"
	echo "utworzony"
else
	echo "już istnieje"
fi

# --------------------------------------------------------------------------- #
log "Kod źródłowy w $APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then
	# git odmawia pracy, gdy katalog należy do kogoś innego niż wywołujący —
	# dlatego wszystkie operacje robimy konsekwentnie jako $APP_USER
	sudo -u "$APP_USER" git -C "$APP_DIR" fetch --all --prune -q
	sudo -u "$APP_USER" git -C "$APP_DIR" reset --hard "origin/$BRANCH" -q
	echo "zaktualizowany do $(sudo -u "$APP_USER" git -C "$APP_DIR" rev-parse --short HEAD)"
else
	mkdir -p "$APP_DIR"
	chown "$APP_USER:$APP_USER" "$APP_DIR"
	sudo -u "$APP_USER" git clone -q --branch "$BRANCH" "$REPO" "$APP_DIR"
	echo "sklonowany"
fi
mkdir -p "$APP_DIR/data"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# --------------------------------------------------------------------------- #
log "Środowisko Pythona"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
	sudo -u "$APP_USER" "$PYTHON" -m venv "$APP_DIR/.venv"
fi
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
echo "zależności zainstalowane"

# --------------------------------------------------------------------------- #
log "Konfiguracja"
if [[ ! -f "$APP_DIR/.env" ]]; then
	SALT="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)"
	cat > "$APP_DIR/.env" <<ENV
OGL_DATABASE_URL=sqlite:///$APP_DIR/data/metruj.db
OGL_DEFAULT_VOIVODESHIP=opolskie
OGL_WEB_HOST=127.0.0.1
OGL_WEB_PORT=$PORT
OGL_MASK_PHONES=true
OGL_PHONE_HASH_SALT=$SALT
OGL_RESPECT_ROBOTS=true
DOMAIN=$DOMAIN
ENV
	chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
	chmod 600 "$APP_DIR/.env"
	echo ".env utworzony (sól do haszowania numerów wylosowana)"
else
	echo ".env już istnieje — nie ruszam"
fi

sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && .venv/bin/python -m metruj.cli init-db" >/dev/null
echo "baza zainicjowana"

# --------------------------------------------------------------------------- #
log "Usługi systemd"
cat > /etc/systemd/system/metruj-web.service <<UNIT
[Unit]
Description=Metruj — interfejs i API
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/uvicorn metruj.api:app --host 127.0.0.1 --port $PORT --workers 2 --proxy-headers
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR/data

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/metruj-scan.service <<UNIT
[Unit]
Description=Metruj — szybki przebieg zbierania ofert
# Zwykły skan i dobowe pełne przejście nie mogą chodzić naraz — oba piszą
# do tej samej bazy i do cache'u geokodowania.
Conflicts=metruj-deep.service
After=metruj-deep.service

[Service]
Type=oneshot
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python -m metruj.cli scan
ExecStartPost=$APP_DIR/.venv/bin/python -m metruj.cli geocode --limit 400
TimeoutStartSec=3600
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR/data
UNIT

cat > /etc/systemd/system/metruj-scan.timer <<UNIT
[Unit]
Description=Metruj — zbieranie nowych ofert co 15 minut

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
RandomizedDelaySec=120
Persistent=true

[Install]
WantedBy=timers.target
UNIT

# Pełne przejście wyników raz na dobę — zwykły skan bierze tylko nowości.
cat > /etc/systemd/system/metruj-deep.service <<UNIT
[Unit]
Description=Metruj — pełne przejście wyników (dobowe)
Conflicts=metruj-scan.service

[Service]
Type=oneshot
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python -m metruj.cli scan --deep --no-details
ExecStartPost=$APP_DIR/.venv/bin/python -m metruj.cli geocode --limit 3000
TimeoutStartSec=21600
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR/data
UNIT

cat > /etc/systemd/system/metruj-deep.timer <<UNIT
[Unit]
Description=Metruj — pełne przejście wyników co dobę

[Timer]
OnCalendar=*-*-* 03:20:00
RandomizedDelaySec=900
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now metruj-web >/dev/null 2>&1
systemctl restart metruj-web
systemctl enable --now metruj-scan.timer metruj-deep.timer >/dev/null 2>&1
echo "usługi uruchomione"

log "Czy aplikacja odpowiada"
for _ in $(seq 1 30); do
	if curl -fsS --max-time 5 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
		curl -fsS "http://127.0.0.1:$PORT/api/health"; echo; break
	fi
	sleep 2
done
curl -fsS --max-time 5 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 \
	|| { journalctl -u metruj-web -n 30 --no-pager; die "aplikacja nie wstała"; }

# --------------------------------------------------------------------------- #
if command -v nginx >/dev/null 2>&1 && [[ -d /etc/nginx/sites-available ]]; then
	log "nginx — vhost dla $DOMAIN"
	NGINX_WAS_OK=0
	nginx -t >/dev/null 2>&1 && NGINX_WAS_OK=1
	[[ $NGINX_WAS_OK -eq 1 ]] || warn "nginx miał błędy JESZCZE PRZED zmianą"

	VHOST="/etc/nginx/sites-available/$DOMAIN"
	[[ -e "$VHOST" ]] && cp -a "$VHOST" "$VHOST.bak.$(date +%s)"
	sed -e "s/bot\.wisnia\.dev/$DOMAIN/g" -e "s/127\.0\.0\.1:8000/127.0.0.1:$PORT/g" \
		"$APP_DIR/deploy/nginx-vhost.conf" > "$VHOST"
	ln -sf "$VHOST" "/etc/nginx/sites-enabled/$DOMAIN"
	mkdir -p /var/cache/nginx/metruj
	chown -R www-data:www-data /var/cache/nginx/metruj 2>/dev/null || true

	if nginx -t 2>/dev/null; then
		systemctl reload nginx
		echo "przeładowany; pozostałe witryny bez zmian"
	else
		warn "test konfiguracji nie przeszedł — WYCOFUJĘ swoją zmianę"
		rm -f "/etc/nginx/sites-enabled/$DOMAIN"
		[[ $NGINX_WAS_OK -eq 1 ]] && nginx -t >/dev/null 2>&1 && systemctl reload nginx
		die "vhost wycofany; aplikacja działa na 127.0.0.1:$PORT"
	fi

	if [[ -n "$ACME_EMAIL" ]]; then
		log "Certyfikat Let's Encrypt"
		if [[ -d "/etc/letsencrypt/live/$DOMAIN" ]]; then
			echo "certyfikat już istnieje"
		else
			command -v certbot >/dev/null || apt-get install -y -qq certbot python3-certbot-nginx
			certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
				-m "$ACME_EMAIL" --redirect \
				|| warn "certbot nie dał rady — sprawdź DNS, potem: certbot --nginx -d $DOMAIN"
		fi
	else
		warn "bez e-maila nie wystawiam certyfikatu: certbot --nginx -d $DOMAIN"
	fi
else
	warn "brak nginxa — aplikacja działa na 127.0.0.1:$PORT"
fi

log "Gotowe"
echo "  strona:   https://$DOMAIN"
echo "  API:      https://$DOMAIN/api/health"
echo "  dokum.:   https://$DOMAIN/docs"
echo
echo "  logi:         journalctl -u metruj-web -f"
echo "  zbieranie:    journalctl -u metruj-scan -f"
echo "  harmonogram:  systemctl list-timers 'metruj*'"
echo "  aktualizacja: sudo bash $APP_DIR/deploy/install.sh $DOMAIN ${ACME_EMAIL:-}"
