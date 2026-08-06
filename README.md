# HomeAssistant-Claude-Skill

Skill Claude Code do odpytywania REST API Home Assistant (stany encji, historia,
logbook, konfiguracja, kalendarze itd.) wraz z testami jednostkowymi.

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
.env_example                            # szablon zmiennych środowiskowych
.gitignore
```

## Wymagania wstępne

- Python 3.12+
- Działająca instancja Home Assistant z wygenerowanym długoterminowym tokenem
  dostępu (Profil użytkownika → Tokeny długoterminowe w HA)

## Konfiguracja środowiska

1. Skopiuj plik przykładowy i uzupełnij własnymi danymi:

   ```bash
   cp .env_example .env
   ```

2. Uzupełnij `.env`:

   ```
   HA_TOKEN=twoj-dlugoterminowy-token
   HA_URL=http://adres-twojego-home-assistant:8123
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

## Uruchamianie testów

```bash
.venv/bin/python -m pytest .claude/skills/ha-api-poll/scripts/test_poll.py -v
```

Testy mockują wszystkie żądania HTTP (`requests.Session`), więc nie łączą się
z prawdziwą instancją Home Assistant i można je uruchamiać bez działającego
serwera.

## Bezpieczeństwo

- Hook w `.claude/settings.json` blokuje żądania HTTP `DELETE` wykonywane
  przez Claude Code w tym projekcie, aby zapobiec przypadkowemu usunięciu
  encji.
- Skill `ha-api-poll` obsługuje wyłącznie odczyt (GET) — wywoływanie usług
  lub zmiana stanu encji (POST) wymaga świadomej, osobnej decyzji.
