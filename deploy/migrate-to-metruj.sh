#!/usr/bin/env bash
# Przeniesienie działającej instalacji „ogloszenia" na „metruj".
#
#   sudo bash deploy/migrate-to-metruj.sh
#
# Uruchamiane **raz**, na serwerze, po wgraniu nowego kodu. Kolejne
# uruchomienia nic nie psują: każdy krok sprawdza, czy nie jest już zrobiony.
#
# Co się zmienia:
#   /opt/ogloszenia        -> /opt/metruj   (dowiązanie starej ścieżki zostaje)
#   użytkownik ogl         -> zostaje, dodajemy metruj i przepisujemy własność
#   ogloszenia-*.service   -> metruj-*.service
#   data/ogloszenia.db     -> data/metruj.db
#
# Baza NIE jest kasowana ani przebudowywana — zmienia się tylko nazwa pliku,
# a schemat domyka `metruj.cli init-db`, dokładając brakujące kolumny.
set -euo pipefail

OLD_DIR=/opt/ogloszenia
NEW_DIR=/opt/metruj
OLD_USER=ogl
NEW_USER=metruj

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "Uruchom przez sudo." >&2; exit 1; }

say "Zatrzymuję stare usługi"
for unit in ogloszenia-web ogloszenia-scan.timer ogloszenia-deep.timer; do
	systemctl stop "$unit" 2>/dev/null || true
	systemctl disable "$unit" 2>/dev/null || true
done

say "Przenoszę katalog"
if [[ -d "$OLD_DIR" && ! -L "$OLD_DIR" ]]; then
	mv "$OLD_DIR" "$NEW_DIR"
	ln -sfn "$NEW_DIR" "$OLD_DIR"     # stare ścieżki w cudzych skryptach mają działać
elif [[ ! -d "$NEW_DIR" ]]; then
	echo "Nie ma ani $OLD_DIR, ani $NEW_DIR — nie ma czego przenosić." >&2
	exit 1
fi

say "Zakładam użytkownika $NEW_USER"
id -u "$NEW_USER" >/dev/null 2>&1 || useradd --system --home-dir "$NEW_DIR" --shell /usr/sbin/nologin "$NEW_USER"
chown -R "$NEW_USER:$NEW_USER" "$NEW_DIR"

say "Przenoszę bazę"
if [[ -f "$NEW_DIR/data/ogloszenia.db" && ! -f "$NEW_DIR/data/metruj.db" ]]; then
	# WAL i shm muszą pójść razem z plikiem bazy, inaczej SQLite uzna je za obce
	for suffix in "" "-wal" "-shm"; do
		[[ -f "$NEW_DIR/data/ogloszenia.db$suffix" ]] &&
			mv "$NEW_DIR/data/ogloszenia.db$suffix" "$NEW_DIR/data/metruj.db$suffix"
	done
	chown -R "$NEW_USER:$NEW_USER" "$NEW_DIR/data"
fi

say "Poprawiam .env"
if [[ -f "$NEW_DIR/.env" ]]; then
	sed -i "s#${OLD_DIR}#${NEW_DIR}#g; s#ogloszenia\.db#metruj.db#g" "$NEW_DIR/.env"
	grep -q '^METRUJ_\|^OGL_' "$NEW_DIR/.env" || echo "OGL_VOIVODESHIPS=wszystkie" >> "$NEW_DIR/.env"
	# Zasięg: serwis obejmuje teraz cały kraj, a stary wpis zawężał go do jednego
	# województwa i po aktualizacji zbierałby dalej samo Opolskie.
	sed -i "s#^OGL_DEFAULT_VOIVODESHIP=.*#OGL_VOIVODESHIPS=wszystkie#" "$NEW_DIR/.env"
	chown "$NEW_USER:$NEW_USER" "$NEW_DIR/.env"
	chmod 600 "$NEW_DIR/.env"
fi

say "Instaluję nowe usługi"
install -m 644 "$NEW_DIR"/deploy/systemd/metruj-*.service /etc/systemd/system/
install -m 644 "$NEW_DIR"/deploy/systemd/metruj-*.timer /etc/systemd/system/
for unit in ogloszenia-web ogloszenia-scan ogloszenia-deep; do
	rm -f "/etc/systemd/system/${unit}.service" "/etc/systemd/system/${unit}.timer"
done
systemctl daemon-reload

say "Domykam schemat bazy i naprawiam dane sprzed zmiany"
# Pakiet nie jest instalowany do środowiska, tylko uruchamiany z drzewa
# źródeł (tak robi też systemd przez WorkingDirectory) — bez `cd` Python
# nie ma go na ścieżce importów.
cd "$NEW_DIR"
sudo -u "$NEW_USER" "$NEW_DIR/.venv/bin/pip" install -q -r "$NEW_DIR/requirements.txt"
sudo -u "$NEW_USER" env -C "$NEW_DIR" "$NEW_DIR/.venv/bin/python" -m metruj.cli init-db
sudo -u "$NEW_USER" env -C "$NEW_DIR" "$NEW_DIR/.venv/bin/python" -m metruj.cli napraw

say "Startuję"
systemctl enable --now metruj-web metruj-scan.timer metruj-deep.timer metruj-kontakty.timer
systemctl is-active metruj-web

say "Gotowe"
systemctl list-timers --all --no-legend | grep metruj || true
echo "Sprawdź: curl -s localhost:8000/api/health"
