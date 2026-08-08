# Plan: uogólnienie ghost_report.py

**Status: zrealizowany 8 sierpnia 2026.** Sekcje 1–4 opisują stan sprzed
przeróbki i zostają jako zapis diagnozy; sekcja 5 zawiera podjęte decyzje,
sekcja 6 — co z nich wyszło. Sekcja 7 to wciąż otwarte obserwacje.
**Cel:** `scripts/ghost_report.py` ma przestać być raportem o jednej konkretnej
instalacji Home Assistanta, a stać się narzędziem, które czyta dowolną instalację
i samo odkrywa jej strukturę. Ktoś, kto sklonuje repo z GitHuba, ma dostać
sensowny raport bez dotykania kodu.

---

## 1. Kontekst — co już istnieje

`scripts/ghost_report.py` znajduje **duchy**: encje, które mają wiersze w bazie
recordera (`states_meta`), ale nie istnieją już w rejestrze encji
(`/api/states`). Rejestr żyje w `.storage/core.entity_registry`, nie w bazie,
więc różnicy nie da się policzyć samym SQL-em — skrypt czyta dwa źródła i
porównuje je w Pythonie.

Skrypt grupuje duchy, generuje plan czyszczenia (`recorder.purge_entities`)
i renderuje samodzielną stronę HTML.

**Pliki:**

| ścieżka | rola |
|---|---|
| `scripts/ghost_report.py` | całość: pobranie, analiza, render |
| `scripts/test_ghost_report.py` | 22 testy; cała suita repo: 48 |
| `reports/ghost-report.html` | wynik (katalog `reports/` jest w `.gitignore`; przed przeróbką `duchy-recordera.html`) |
| `reports/snapshot.json` | surowe dane, wejście dla `--from-snapshot` |
| `.env` | `HA_URL`, `HA_TOKEN`, oraz `HA_RECORDER_DB` **albo** `SQLITE_WEB_URL` |

**Uruchomienie:**

```bash
.venv/bin/python scripts/ghost_report.py --db ~/config/home-assistant_v2.db
.venv/bin/python scripts/ghost_report.py --from-snapshot reports/snapshot.json --lang pl
.venv/bin/python -m pytest scripts/ -q
```

**Dane referencyjne (8 sierpnia 2026):** `states_meta` 291, żywych 184,
duchów 107, 102 401 wierszy = 1,2 % tabeli `states`. Wcześniej tego samego dnia
było 635 / 186 / 449 — właściciel wykonał purgę 344 encji planem z raportu.

> **Uwaga o prywatności.** Ten dokument jest w publicznym repozytorium, więc
> nie ma w nim nazw urządzeń, hostname'ów ani szczegółów automatyzacji. Wszystko
> to siedzi w `docs/plan-generalizacja.local.md`, który jest w `.gitignore`.
> Jeśli czytasz to jako osoba klonująca repo — tamtego pliku po prostu nie ma
> i niczego nie tracisz.

---

## 2. Diagnoza — trzy warstwy przywiązania do jednej instalacji

### Warstwa 1: literalne identyfikatory sprzętu

Siedem miejsc w `scripts/ghost_report.py` zawierało nazwy wzięte wprost
z instalacji właściciela: hostname serwera w globie aktualizacji Dockera,
model telewizora w regule grupującej, nazwa telefonu, nazwy lokalnych modeli
językowych, oznaczenie oprawy IKEA i trzy marki sprzętu AV. Konkretne ciągi
i numery linii: `docs/plan-generalizacja.local.md`.

U kogokolwiek innego te reguły nie dopasują niczego — wszystkie duchy wpadną
do kosza „Pozostałe", a raport straci swoją główną wartość: pokazanie, że
setki encji przyszły z jednej usuniętej integracji.

### Warstwa 2: ukryte założenia

- **`DOCKER_SUFFIXES` (linia ~81) i sekcja „Rodziny Dockera"** zakładają, że
  użytkownik ma integrację monitorującą kontenery, i to konkretnie tę,
  z nazewnictwem `<kontener>_cpu_usage_total`, `<kontener>_container` itd.
- **Sekcja renderuje się bezwarunkowo (linia ~686).** Bez Dockera użytkownik
  dostaje nagłówek „Rodziny Dockera — 0 encji", pustą tabelę i „0 kontenerów".
  Wygląda to na zepsute narzędzie, nie na pusty wynik.
- **`SQLITE_WEB_URL` jest wymagane.** Większość użytkowników HA nie ma
  postawionego sqlite_web.
- **Zapytanie o rozmiar bazy używa `pragma_page_count()` / `pragma_page_size()`.**
  Sporo instalacji trzyma recorder w MariaDB albo PostgreSQL, gdzie to nie
  istnieje i zapytanie się wywali.

### Warstwa 3: treść strony

Polskie napisy, ale też narracja będąca wnioskiem z konkretnej diagnozy:
„lawina discovery", „Cast klonuje encję przy każdym wykryciu",
„Wyparte przez encje `_2`". To nie są prawdy uniwersalne.

---

## 3. Zasada projektowa: grupy mają wynikać z danych

Zamiast ręcznych reguł — trzy sygnały, które daje sama baza:

**a) Kohorta czasu zgonu.** Najważniejszy sygnał. Encje usunięte razem mają
niemal identyczny `MAX(last_updated_ts)`. W danych referencyjnych wszystkie
kontenerowe zamilkły o 09:48:31, sensory telefonu 28 maja, Ollama 5 sierpnia.
Klastrowanie po tym znaczniku odtwarza większość ręcznych grup **bez ani jednej
nazwy sprzętu w kodzie**. Etykieta powstaje sama: „47 encji, które zniknęły
8 sierpnia o 09:48".

**b) Rodziny sufiksów.** Zamiast listy `DOCKER_SUFFIXES` — wykrywać sufiksy
występujące u co najmniej N duchów przy co najmniej M różnych prefiksach.
`_cpu_usage_total` przy dwudziestu prefiksach to rodzina; `_screen_state` przy
jednym to nie. Tabela przestaje być „o Dockerze", a staje się „o powtarzalnych
wzorcach nazw".

**c) Wspólne tokeny.** Model urządzenia bywa wpisany w nazwę pięćdziesięciu
encji naraz — to wystarczy, by zrobić z nich grupę i nazwać ją tym tokenem,
bez wiedzy, jakim urządzeniem był.

**Globi też mają być generowane**, nie wpisane. `CANDIDATE_GLOBS` powstaje
z wykrytych rodzin sufiksów. Mechanizm bezpieczeństwa zostaje bez zmian:
każdy kandydat sprawdzany względem żywych encji, kolizja = odrzucenie
i przeniesienie duchów na listę jawną.

---

## 4. Co NIE MOŻE się zepsuć

- **Własność bezpieczeństwa:** żaden wyemitowany glob nie może pasować do
  żywej encji. Testy `TestBuildPurgePlan` to pilnują — muszą przejść bez zmian
  w asercjach.
- **Partycjonowanie:** każdy duch dokładnie raz, albo w `covered`, albo
  w `explicit`. Suma musi się zgadzać z liczbą duchów.
- **Strażnik SELECT-only** (`SQL_ALLOWED`) — do bazy trafiają wyłącznie
  zapytania odczytujące.
- **Trójstanowy motyw strony** — `:root`, `@media (prefers-color-scheme: dark)`
  z gardą `:not([data-theme="light"])`, oraz `:root[data-theme="dark"]`.
  Pilnuje tego `test_theme_tokens_are_all_defined_in_base_root`.
- **Domknięte `<details>`** i obecność `<title>`.

---

## 5. Decyzje — ROZSTRZYGNIĘTE 8 sierpnia 2026

1. **Język interfejsu:** domyślnie **angielski**, przełącznik `--lang pl`
   wraca do polskiego. Teksty raportu wyprowadzone do słownika `STRINGS`.
   Podsumowanie na stdout zostaje po angielsku niezależnie od flagi — jest
   diagnostyczne, nie prezentacyjne.

2. **Specyfika właściciela: wyrzucona całkowicie.** Bez
   `ghost_report.local.toml`, bez `GROUP_RULES`, bez `DOCKER_SUFFIXES`, bez
   wpisanych na sztywno globów. Grupy i globy powstają wyłącznie z danych.
   Konsekwencja przyjęta świadomie: zamiast opisowego „Telewizor Philips"
   pojawia się „Wspólny człon nazwy: &lt;model&gt;".

3. **Dostęp do bazy:** dodane `--db /ścieżka/home-assistant_v2.db` przez
   `sqlite3` ze stdliba (otwarcie w trybie `mode=ro`), `SQLITE_WEB_URL`
   przestaje być wymagane. MariaDB/PostgreSQL **nie są wspierane** — DSN
   z takim schematem jest wykrywany i odrzucany komunikatem.

4. **Zakres:** README idzie razem ze skryptem.

---

## 6. Co zostało zrobione

1. Wykrywanie struktury wydzielone do `death_cohorts`, `affix_families`
   i `shared_tokens`, każde z własnymi testami na danych syntetycznych.
   Rodziny wykrywają nie tylko sufiksy, ale i prefiksy — to stamtąd biorą się
   dawne globy `sensor.local_*` i `update.docker_images_*`.
2. `GROUP_RULES` i `DOCKER_SUFFIXES` usunięte. `group_ghosts` przypisuje
   każdego ducha dokładnie raz: najpierw wspólne człony nazw, potem kohorty
   czasu zgonu, na końcu kosz. Pierwszeństwo zachowane —
   `test_shared_token_wins_over_a_generic_suffix_family` pilnuje przypadku
   `switch.<tv>_screen_state`.
   Tokeny z sufiksów rodzin są wykluczane z grupowania (żeby `usage` czy
   `total` nie stały się grupą), z prefiksów **nie** — tam siedzi tożsamość
   usuniętego urządzenia.
3. `CANDIDATE_GLOBS` zastąpione funkcją `candidate_globs`, generującą wzorce
   z rodzin i z grup tokenowych. Weryfikacja kolizji nietknięta.
4. Sekcje HTML renderowane przez `render_section`, które zwraca pusty ciąg
   przy pustym ciele — nagłówek nie pojawia się bez danych.
5. Teksty w `STRINGS` (`en`, `pl`), wybór przez `--lang`. Separator tysięcy
   i przecinek dziesiętny idą za językiem. Podsumowanie na stdout zostało
   po angielsku.
6. Odczyt bazy przez `--db` / `HA_RECORDER_DB` (`sqlite3`, `mode=ro`).
   DSN MariaDB/PostgreSQL wykrywany regexem i odrzucany.
7. `build_purge_plan(ghosts, live, globs)` — globy są teraz argumentem, nie
   stałą modułu. Asercje `TestBuildPurgePlan` zostały bez zmian, zmieniło się
   tylko wywołanie: testy podają globy jawnie, więc sprawdzają weryfikator,
   a nie to, co akurat wypluł detektor.
8. `TestRealSnapshot` odtwarza `reports/snapshot.json`, jeśli plik istnieje
   (jest gitignorowany, więc u klonującego klasa się pomija).

**Wynik na danych referencyjnych (107 duchów, bez ani jednej nazwy w kodzie):**

| sygnał, który utworzył grupę | encji |
|---|---|
| wspólny człon nazwy (model telewizora) | 37 |
| kohorta czasu zgonu (kontenery) | 32 |
| kohorta czasu zgonu (sensory telefonu) | 14 |
| kohorta czasu zgonu (modele językowe) | 11 |
| dwa dalsze wspólne człony nazw | 4 + 4 |
| kosz „Pozostałe" | 5 |

Automat odtworzył dawny podział ręczny; do kosza wpadło 5 encji ze 107.
Rozpisanie na konkretne urządzenia: `docs/plan-generalizacja.local.md`.

Plan czyszczenia jest teraz uboższy niż przed przeróbką (8 encji pod dwoma
globami, 99 na liście jawnej) i **tak ma być**: po sierpniowej purdze zostały
głównie te duchy, których wzorce kolidują z żywymi encjami — `sensor.*_state`
trafia w żywy `sensor.backup_backup_manager_state`, `sensor.*_memory_usage`
w `sensor.system_monitor_memory_usage`, a globy oparte na modelu telewizora
w encje, które Cast wciąż odtwarza. Wszystkie są odrzucane automatycznie
i widoczne w raporcie.

---

## 7. Znane, niezałatwione obserwacje z instalacji właściciela

Cztery zdiagnozowane usterki czekające na decyzję: martwa reguła w szablonie
pogodowym, luka w harmonogramie jednej automatyzacji, integracja klonująca
encje przy każdym wykryciu i glob trwale kolidujący z żywą encją.

Opisy zawierają nazwy encji i godziny działania automatyzacji, więc żyją
w `docs/plan-generalizacja.local.md` (poza repozytorium).
