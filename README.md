# HomeAssistant-Claude-Skill

Dwa niezależne narzędzia do jednej instancji Home Assistanta:

- **skill `ha-api-poll`** — odpytywanie REST API (stany encji, historia,
  logbook, konfiguracja, kalendarze) z poziomu Claude Code,
- **`scripts/ghost_report.py`** — audyt bazy recordera: znajduje encje, po
  których zostały wiersze w bazie, choć zniknęły z rejestru, i generuje
  bezpieczny plan czyszczenia.

Oba mają testy jednostkowe i oba czytają wyłącznie — żadne z nich nie zmienia
stanu Home Assistanta.

## Struktura projektu

```
.claude/
  settings.json                        # hooki projektu (m.in. blokada żądań DELETE)
  skills/
    ha-api-poll/
      SKILL.md                         # opis skilla dla Claude Code
      scripts/
        poll.py                        # skrypt odpytujący API
        test_poll.py                   # testy jednostkowe (pytest)
      references/
        api-reference.md               # pełna referencja API HA
scripts/
  ghost_report.py                      # audyt osieroconych encji recordera
  test_ghost_report.py                 # testy jednostkowe (pytest)
reports/                               # wygenerowane raporty (w .gitignore)
.env_example                           # szablon zmiennych środowiskowych
.gitignore
```

## Wymagania wstępne

- Python 3.12+
- Działająca instancja Home Assistant z wygenerowanym długoterminowym tokenem
  dostępu (Profil użytkownika → Tokeny długoterminowe w HA)
- Do `ghost_report.py`: dostęp do pliku `home-assistant_v2.db` (recorder na
  SQLite; MariaDB i PostgreSQL nie są obsługiwane)

## Konfiguracja środowiska

1. Skopiuj plik przykładowy i uzupełnij własnymi danymi:

   ```bash
   cp .env_example .env
   ```

2. Uzupełnij `.env`:

   ```
   HA_TOKEN=twoj-dlugoterminowy-token
   HA_URL=http://adres-twojego-home-assistant:8123

   # tylko dla ghost_report.py — jedno z dwóch
   HA_RECORDER_DB=/sciezka/do/config/home-assistant_v2.db
   # SQLITE_WEB_URL=http://adres-twojego-home-assistant:8088
   ```

   Plik `.env` jest w `.gitignore` — nigdy nie trafi do repozytorium.

3. Utwórz wirtualne środowisko i zainstaluj zależności (system Python jest
   "externally managed", więc pakiety instalujemy w `.venv`, nie globalnie):

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install --upgrade pip pytest requests
   ```

## Użycie skilla `ha-api-poll`

Skrypt `poll.py` odczytuje dane logowania z `.env` i wypisuje odpowiedź API
jako JSON.

```bash
# Sprawdzenie, czy API działa
.venv/bin/python .claude/skills/ha-api-poll/scripts/poll.py ping

# Pełna konfiguracja systemu
.venv/bin/python .claude/skills/ha-api-poll/scripts/poll.py config

# Stan wszystkich encji lub jednej konkretnej
.venv/bin/python .claude/skills/ha-api-poll/scripts/poll.py states
.venv/bin/python .claude/skills/ha-api-poll/scripts/poll.py states light.office

# Historia zmian encji
.venv/bin/python .claude/skills/ha-api-poll/scripts/poll.py history sensor.temperature \
  --start 2026-07-31T00:00:00+00:00

# Wpisy z logbooka dla konkretnej encji
.venv/bin/python .claude/skills/ha-api-poll/scripts/poll.py logbook --entity light.office

# Lista kalendarzy i wydarzenia w danym zakresie dat
.venv/bin/python .claude/skills/ha-api-poll/scripts/poll.py calendars
.venv/bin/python .claude/skills/ha-api-poll/scripts/poll.py calendar calendar.home \
  --start 2026-08-01T00:00:00 --end 2026-08-08T00:00:00

# Dowolny endpoint GET nieobjęty powyższymi poleceniami
.venv/bin/python .claude/skills/ha-api-poll/scripts/poll.py get /api/services
```

Pełną listę endpointów (w tym POST/DELETE, których ten skrypt celowo nie
obsługuje) znajdziesz w `.claude/skills/ha-api-poll/references/api-reference.md`.

W Claude Code skill uruchamia się automatycznie, gdy poprosisz np. "sprawdź
stan światła w biurze" albo "co się działo z czujnikiem temperatury" — nie
trzeba wywoływać `poll.py` ręcznie.

## Raport `ghost_report.py` — duchy recordera

**Duch** to `entity_id`, który wciąż ma wiersze w tabeli `states` bazy
recordera, ale nie istnieje już w rejestrze encji. Rejestr żyje w
`.storage/core.entity_registry`, nie w bazie, więc różnicy nie da się policzyć
samym SQL-em: skrypt czyta `/api/states` i bazę, porównuje je i renderuje
samodzielną stronę HTML z planem czyszczenia.

```bash
# raport po angielsku do reports/ghost-report.html
.venv/bin/python scripts/ghost_report.py --db ~/config/home-assistant_v2.db

# po polsku, w wybrane miejsce
.venv/bin/python scripts/ghost_report.py --lang pl --out /tmp/duchy.html

# zapisz surowe dane, żeby móc przebudować raport bez sieci
.venv/bin/python scripts/ghost_report.py --snapshot reports/snapshot.json
.venv/bin/python scripts/ghost_report.py --from-snapshot reports/snapshot.json

# sam plan czyszczenia na stdout, do wklejenia w Narzędzia deweloperskie
.venv/bin/python scripts/ghost_report.py --print-plan

# bez adresu bazy w nagłówku — gdy stronę ma zobaczyć ktoś jeszcze
.venv/bin/python scripts/ghost_report.py --hide-source
```

Ścieżkę do bazy można podać flagą `--db` albo zmienną `HA_RECORDER_DB`;
zamiast tego działa też `SQLITE_WEB_URL`, jeśli masz postawiony sqlite_web.

### Zanim pokażesz raport komuś innemu

Strona zawiera pełną listę `entity_id` z Twojej instalacji, a domyślnie także
adres bazy, z której powstała. `--hide-source` usuwa z nagłówka sam adres;
inwentarza encji nie usuwa nic — to jest treść raportu. Katalog `reports/`
jest w `.gitignore`, więc raporty i snapshoty nie trafiają do repozytorium.

### Skąd biorą się grupy w raporcie

W skrypcie nie ma ani jednej nazwy urządzenia czy integracji. Struktura jest
odkrywana z danych, trzema sygnałami:

- **kohorty czasu zgonu** — encje usunięte jedną akcją przestają pisać
  w odstępie sekund, więc dłuższa cisza między znacznikami to szew między
  dwoma osobnymi zdarzeniami;
- **rodziny sufiksów i prefiksów** — `_cpu_usage_total` przy dwudziestu
  różnych nazwach to wzorzec, `_screen_state` przy jednej to przypadek;
- **wspólne człony nazw** — fragment obecny u wielu duchów i u prawie żadnej
  żywej encji identyfikuje to, co zniknęło.

### Bezpieczeństwo planu czyszczenia

Globi do `recorder.purge_entities` są **generowane** z wykrytych wzorców,
a potem każdy z nich jest sprawdzany względem pełnej listy żywych encji.
Jedno trafienie w żywą encję wystarczy, by glob został odrzucony — jego duchy
przechodzą wtedy na listę jawną, a odrzucenie jest widoczne w raporcie.
Plan jest więc bezpieczny z konstrukcji, nie z przeglądu. Do bazy trafiają
wyłącznie zapytania `SELECT`, a przy dostępie przez `--db` połączenie jest
otwierane w trybie `mode=ro`.

Raport nie usuwa niczego sam — wypisuje YAML do świadomego wklejenia
w Narzędzia deweloperskie → Akcje.

## Uruchamianie testów

```bash
.venv/bin/python -m pytest .claude/skills/ha-api-poll/scripts/test_poll.py -v
.venv/bin/python -m pytest scripts/ -v
```

Testy mockują wszystkie żądania HTTP (`requests.Session`), więc nie łączą się
z prawdziwą instancją Home Assistant i można je uruchamiać bez działającego
serwera. Testy `ghost_report.py` pracują na danych syntetycznych; dodatkowa
klasa odtwarza `reports/snapshot.json`, jeśli taki plik lokalnie istnieje.

## Bezpieczeństwo

- Hook w `.claude/settings.json` blokuje żądania HTTP `DELETE` wykonywane
  przez Claude Code w tym projekcie, aby zapobiec przypadkowemu usunięciu
  encji.
- Skill `ha-api-poll` obsługuje wyłącznie odczyt (GET) — wywoływanie usług
  lub zmiana stanu encji (POST) wymaga świadomej, osobnej decyzji.
- `ghost_report.py` wysyła do bazy wyłącznie `SELECT`, a przy `--db` otwiera
  plik w trybie tylko do odczytu. Plan czyszczenia jest generowany, nigdy
  wykonywany.
