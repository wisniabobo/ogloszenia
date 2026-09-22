# Wdrożenie

## Najprościej: jedna komenda na serwerze

Zaloguj się na serwer i wklej:

```bash
curl -fsSL https://raw.githubusercontent.com/wisniabobo/ogloszenia/main/deploy/bootstrap.sh \
  | sudo bash -s -- twoja.domena.pl twoj@email.pl
```

Skrypt jest **idempotentny** — tą samą komendą aktualizujesz instalację.

Co robi, po kolei:

1. sprawdza, czy domena wskazuje na ten serwer,
2. instaluje Dockera, jeśli go nie ma,
3. klonuje repozytorium do `/opt/ogloszenia`,
4. tworzy `.env` z **wylosowaną** solą do haszowania numerów telefonów,
5. uruchamia aplikację na **127.0.0.1:8000** — porty 80 i 443 zostają nietknięte,
6. jeśli na serwerze jest nginx, dokłada vhosta **tylko dla podanej domeny**
   i przeładowuje konfigurację dopiero po udanym `nginx -t`,
7. jeśli podałeś e-mail, wystawia certyfikat Let's Encrypt dla tej jednej domeny,
8. robi pierwszy skan i geokodowanie.

### Dlaczego aplikacja nie zajmuje portów 80/443

Bo na serwerze zwykle coś już na nich stoi — choćby nginx obsługujący inne
strony. Gdyby aplikacja przejęła te porty, przestałyby działać. Dlatego słucha
wyłącznie na pętli zwrotnej, a ruch z internetu kieruje do niej istniejący nginx.

Na **czystym** serwerze, bez własnego nginxa, można zamiast tego włączyć
wbudowane Caddy (samo wystawia i odnawia certyfikat):

```bash
docker compose --profile caddy up -d
```

## Ręcznie, krok po kroku

```bash
git clone https://github.com/wisniabobo/ogloszenia.git /opt/ogloszenia
cd /opt/ogloszenia
cp .env.example .env && nano .env      # DOMAIN, ACME_EMAIL, OGL_PHONE_HASH_SALT
docker compose up -d --build
docker compose exec web python -m metruj.cli init-db

sudo cp deploy/nginx-vhost.conf /etc/nginx/sites-available/twoja.domena.pl
sudo ln -sf /etc/nginx/sites-available/twoja.domena.pl /etc/nginx/sites-enabled/
sudo mkdir -p /var/cache/nginx/ogloszenia
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d twoja.domena.pl
```

## Usługi

| usługa | rola |
|---|---|
| `web`    | aplikacja (uvicorn, 2 procesy robocze), tylko `127.0.0.1:8000` |
| `worker` | zbieranie ofert wg harmonogramu — osobno, żeby nie spowalniać strony |
| `caddy`  | **opcjonalne**, tylko przy `--profile caddy` |

## Codzienna obsługa

```bash
cd /opt/ogloszenia
docker compose logs -f worker                                   # co robi zbieranie
docker compose exec web python -m metruj.cli stats
docker compose exec web python -m metruj.cli check-sources
docker compose exec web python -m metruj.cli scan --pages 5
docker compose exec web python -m metruj.cli geocode --limit 2000
docker compose restart web                                      # po zmianie .env
```

## Kopia zapasowa

Cała baza to jeden plik:

```bash
docker compose exec web sh -c 'python -c "
import sqlite3, shutil
src = sqlite3.connect(\"/data/metruj.db\")
dst = sqlite3.connect(\"/data/backup.db\")
src.backup(dst); dst.close(); src.close()
"'
docker compose cp web:/data/backup.db ./kopia-$(date +%F).db
```

## Bez Dockera: systemd

```bash
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ogloszenia-web ogloszenia-scan.timer
```

Pliki zakładają projekt w `/opt/ogloszenia`, wirtualne środowisko w `.venv`
i użytkownika `ogl`.

## Skalowanie

SQLite spokojnie obsługuje jedno województwo i kilkuset odwiedzających dziennie,
zwłaszcza z cache'em nginxa przed aplikacją. Przy całej Polsce albo dużym ruchu
przełącz się na PostgreSQL — to zmiana jednej zmiennej:

```ini
OGL_DATABASE_URL=postgresql+psycopg://ogl:haslo@db:5432/ogloszenia
```

## Bezpieczeństwo

- **wyłącz logowanie po haśle do SSH** (`PasswordAuthentication no` w
  `/etc/ssh/sshd_config`) i używaj klucza — hasło, które gdziekolwiek wkleisz,
  trzeba uznać za spalone i natychmiast zmienić (`passwd`),
- `OGL_PHONE_HASH_SALT` musi być losowy; bootstrap losuje go sam,
- rozważ `OGL_STORE_PHONE_HASH_ONLY=true` — deduplikacja działa dalej,
  a numerów telefonów nie ma wtedy w bazie w ogóle,
- aplikacja nie wystawia niczego na świat bezpośrednio: słucha tylko na
  `127.0.0.1`, a na zewnątrz widoczny jest wyłącznie nginx.
