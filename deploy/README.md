# Wdrożenie

## Wariant zalecany: Docker + Caddy (HTTPS sam się robi)

Na serwerze musi być otwarty port 80 i 443, a domena musi wskazywać na jego IP.

```bash
# 1. Klucz SSH zamiast hasła (jednorazowo, z Twojego komputera)
ssh-keygen -t ed25519 -C "deploy ogloszenia"
ssh-copy-id user@TWOJE_IP

# 2. Wdrożenie
./deploy/deploy.sh user@TWOJE_IP
```

Skrypt zainstaluje Dockera (jeśli go nie ma), sklonuje repozytorium do
`/opt/ogloszenia`, zbuduje obraz i wystartuje trzy usługi:

| usługa | rola |
|---|---|
| `web`    | aplikacja (uvicorn, 2 procesy robocze) |
| `worker` | zbieranie ofert wg harmonogramu — osobno, żeby nie spowalniać strony |
| `caddy`  | HTTPS, kompresja, nagłówki cache |

Po pierwszym uruchomieniu uzupełnij `/opt/ogloszenia/.env`:

```ini
DOMAIN=bot.wisnia.dev
ACME_EMAIL=twoj@email.pl
OGL_PHONE_HASH_SALT=<długi losowy ciąg>
```

i zrestartuj: `docker compose up -d`.

### Pierwsze napełnienie bazy

```bash
docker compose exec web python -m ogloszenia.cli scan --pages 5
docker compose exec web python -m ogloszenia.cli geocode --limit 2000
```

### Codzienna obsługa

```bash
docker compose logs -f worker        # co robi zbieranie
docker compose exec web python -m ogloszenia.cli stats
docker compose exec web python -m ogloszenia.cli check-sources
docker compose restart web           # po zmianie .env
```

### Kopia zapasowa

Cała baza to jeden plik w wolumenie `ogl-data`:

```bash
docker compose exec web sh -c 'sqlite3 /data/ogloszenia.db ".backup /data/backup.db"'
docker compose cp web:/data/backup.db ./kopia-$(date +%F).db
```

## Wariant bez Dockera: systemd

Gdy wolisz uruchomić bezpośrednio na serwerze:

```bash
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ogloszenia-web ogloszenia-scan.timer
```

Pliki zakładają, że projekt leży w `/opt/ogloszenia`, ma tam `.venv`
i działa na użytkowniku `ogl`.

## Skalowanie

SQLite spokojnie obsługuje jedno województwo i kilkuset odwiedzających
dziennie. Przy całej Polsce albo dużym ruchu przełącz się na PostgreSQL —
wystarczy zmienić jedną zmienną:

```ini
OGL_DATABASE_URL=postgresql+psycopg://ogl:haslo@db:5432/ogloszenia
```

## Bezpieczeństwo

- logowanie po haśle do SSH: **wyłącz** (`PasswordAuthentication no`) —
  skrypt wdrożeniowy i tak używa wyłącznie klucza,
- `OGL_PHONE_HASH_SALT` musi być losowy i inny niż przykładowy,
- rozważ `OGL_STORE_PHONE_HASH_ONLY=true` — deduplikacja działa dalej,
  a numerów telefonów nie ma wtedy w bazie w ogóle,
- Caddy ustawia HSTS; jeśli domena ma kiedyś działać po HTTP, usuń ten nagłówek.
