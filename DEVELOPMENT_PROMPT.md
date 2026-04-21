# EMS Optimizer — Prompt dla kolejnych sesji deweloperskich

**Projekt:** GoodWe EMS na Home Assistant  
**Wersja:** 2.2.0  
**Status:** Produkcja z iteracjami optymalizacyjnymi  
**Repozytorium:** `yakemishan/gw_ems_optimizer` (GitHub)

---

## 1. Encje Home Assistant i ich użycie

### Wejścia (sensory — dane do optimizera)

| Encja | Typ | Opis | Pytanie |
|-------|-----|------|---------|
| `sensor.solcast_pv_forecast_prognoza_na_dzisiaj` | `sensor` | PV na dziś (36 slotów godzinowych, detailedHourly) | Ile PV będzie w każdej godzinie? |
| `sensor.solcast_pv_forecast_prognoza_na_jutro` | `sensor` | PV na jutro (fallback gdy jutro niedostępne) | Ile PV jutro? |
| `sensor.rce_pse_cena` | `sensor` | Aktualna cena RCE (PLN/MWh) — SPRZEDAŻ | Za ile sprzedać energię teraz? |
| `sensor.rce_pse_cena_jutro` | `sensor` | Ceny RCE na jutro (dostępne od ~13:00) | Za ile sprzedamy jutro? |
| `sensor.battery_state_of_charge` | `sensor` | Aktualny SoC baterii (%) | Ile procent baterii mamy? |
| `sensor.inwerter_dom_dzienne_zuzycie` | `sensor` | Kumulatywne zużycie domu (kWh) — 14 dni średnia | Ile średnio zużywamy na godzinę? |

### Wyjścia (decyzje optimizera do Node-RED)

| Encja | Typ | Opis | Pytanie |
|-------|-----|------|---------|
| `sensor.ems_optimizer_decision` | `sensor` | Stan=aktualny tryb; Atrybuty=plan 36h + decyzja bieżąca | Jaki tryb teraz? Jaki plan na 36h? |
| `sensor.ems_plan_text` | `sensor` | Plan jako tabela markdown (do wyświetlania w HA) | Pokazać plan użytkownikowi |

### Node-RED (Nadzorca — wykonawcze)

| Encja | Typ | Opis | Kto zmienia |
|-------|-----|------|-------------|
| `select.inwerter_dom_ems_mode` | `select` | Aktualny tryb pracy GoodWe | Node-RED (na podstawie planu) |
| `number.inwerter_dom_ems_power_limit` | `number` | Limit mocy (Xset dla GoodWe) | Node-RED (mapowanie mode→Xset) |
| `switch.ciepla_woda_kotlownia_switch` | `switch` | Włącznik grzania wody (tanki moment) | Node-RED (gdy cena <0 lub bardzo niska) |
| `switch.hydrofor_piwnica_socket_1` | `switch` | Włącznik hydroforu (tanki moment) | Node-RED (gdy cena <0 lub bardzo niska) |
| `input_boolean.ems_force_replan` | `input_boolean` | Flaga wymuszenia preplanu | Ręcznie przez UI lub automatyka |

### Logowanie (MySQL — historia dla nauki)

| Tabela | Opis | Kiedy pisana |
|--------|------|-------------|
| `ems_plan_log` | Pełny plan per sesja (36 slotów × wartości LP) | Co 6h o 00/06/12/18 |
| `debug_log` | Anomalie nadzorcy (gdy Node-RED zmienił tryb) | Co minutę jeśli zmiana trybu |
| `ems_accuracy_view` | VIEW: plan vs actual (porównanie) | Do nauki (od ~29-30 kwietnia 2026) |

---

## 2. Tryby pracy EMS i ich wpływ

### Mapowanie Tryb → Xset (GoodWe) → Zachowanie baterii

| Tryb | Xset | Bateria | PV | Import | Export | Zastosowanie |
|------|------|---------|-----|--------|--------|--------------|
| **`auto`** | 0 | Ładuje z PV nadwyżki; rozładowuje gdy brak PV | Pokrywa konsumpcję → bateria | Gdy SoC=10% i brak PV | Gdy nadwyżka PV lub SoC=100% | Default — system sam zarządza |
| **`sell_power`** | 20000W | Rozładowuje do 10%, potem stoi | Eksportuje bezpośrednio | Brak | PV + bateria jednocześnie | Szczyt cenowy (RCE >600 PLN/MWh), SoC>30% |
| **`discharge_battery`** | 4500W | Rozładowuje stałą mocą 4.5kW | Eksportuje nadwyżkę | Brak | Z baterii (~5kWh/h) | Wieczorny szczyt (RCE 600-830 PLN/MWh) |
| **`battery_standby`** | 0 | Stoi (PBat=0) | Eksportuje nadwyżkę bezpośrednio | Gdy PV < konsumpcja | Bezpośrednio z PV | Przed tanim oknem (PV>=3kWh, ceny malejące) |
| **`charge_battery`** | 5000W | Ładuje z sieci (Xset=5000W) + PV | Pokrywa konsumpcję | Stały import 5kWh/h | Brak | Szczyt G13 Pozostałe (0.6256 PLN/kWh) + dużo PV |

### Logika wyboru trybu (LP + okna cenowe)

```
IF window == 'before_min':        # Ceny wysokie/malejące PRZED tanim oknem
  IF pv >= 3kWh:
    → battery_standby             # PV eksportuje, bateria czeka
  ELSE:
    → auto                        # Mało PV, bateria pokrywa

ELIF window == 'cheap':           # Najtańsze ceny (min + 150 PLN/MWh, zawiera ujemne)
  → auto                          # PV ładuje baterię

ELSE:                             # night, after_min
  Standardowa logika LP:
  - Jeśli ch > 0 i imp > 0 → charge_battery
  - Jeśli dis > 0 i exp > 0 i cena > 0 → sell_power (jeśli 7-18) lub discharge_battery
  - Jeśli dis > 0 (sam) → discharge_battery (jeśli >1kWh żądane) lub auto
  - DEFAULT → auto
```

---

## 3. Logika optimizera (LP) i znane problemy

### LP Model (scipy.optimize.linprog)

**Zmienne per slot (4n zmiennych):**
- `ch[j]` — ładowanie baterii (0 do 5 kWh/h)
- `dis[j]` — rozładowanie baterii (0 do 5 kWh/h)
- `imp[j]` — import z sieci (0 do ∞, ale blokowany w szczytach G13)
- `exp[j]` — eksport do sieci (0 do PV + ch)

**Cel (minimalizuj koszt):**
```
Σ [ buy_price[j] · imp[j] - sell_price[j] · exp[j] ]
```

**Ograniczenia:**
1. **Bilans energii:** `ch[j] - dis[j] + imp[j] - exp[j] = pv[j] - cons[j]` (per slot)
2. **Granice SoC:** `min_soc[j] ≤ soc[j] ≤ max_soc` (kumulatywnie od soc_init)
3. **Moc:** `ch[j] + dis[j] ≤ 5kW` (nie cycling jednocześnie)
4. **Import blokada:** `imp[j] = 0` jeśli szczyt G13 i bilans dodatni (zakazujemy kupować na szczycie)
5. **Min SoC:** Obliczany z `_calc_min_soc()` — rezerwa na nocne zużycie

### Znane problemy

#### Problem 1: **Cycling (ch i dis > 0 jednocześnie)** 
- **Objaw:** O 10:00 `ch=2.5, dis=2.5` — SoC stoi w miejscu  
- **Przyczyna:** LP ma logikę `ch + dis ≤ 5` ale przy dużym PV solver wybiera równowagę  
- **Wpływ:** Słoty > 12h mają błędny SoC w planie, ale decyzje bieżące OK (6h sesje recalc z rzeczywistym SoC)  
- **Rozwiązanie:** MILP z binarną zmienną `bat_full[j]` (zaplanowano, nie wdrożone)

#### Problem 2: **SoC przy małym PV w `before_min`**
- **Objaw:** O 06:00 przy PV=0.1kWh plan ustawia `before_min→auto`, ale mogłoby być `standby` gdyby było więcej PV  
- **Logika:** Wymaga PV >= 3kWh dla `standby`, poniżej = `auto`  
- **Wpływ:** Minimalna, bo konsumpcja rano niska  
- **Status:** AKCEPTOWALNE

#### Problem 3: **LP infeasible przy SoC=10%**
- **Objaw:** Scenariusze "SoC=10% nocą" → `INFEASIBLE→safe_auto`  
- **Przyczyna:** Zbyt mała rezerwa, LP nie może spełnić ograniczeń min_soc  
- **Rozwiązanie:** Fallback `_safe_auto_plan()` — cały plan = `auto`, zero import/export  
- **Status:** POPRAWNE ZACHOWANIE

#### Problem 4: **LP DEBUG logi**
- **Status:** Wciąż w kodzie, do usunięcia (`self.log(f"LP DEBUG...")`)

---

## 4. Główne założenia optymalizacji

### Zasady nadrzędne

1. **Nigdy nie kupuj z sieci aby sprzedać** (anti-arbitraż)
   - Import blokowany gdy szczyt G13 Szczyt Przed/Po (0.9047, 1.4959 PLN/kWh)
   - Import dozwolony tylko w G13 Pozostałe (0.6256 PLN/kWh)

2. **Kup tanio, sprzedaj drogo**
   - Ładuj baterię gdy RCE <= min + 150 PLN/MWh (w oknie `cheap`)
   - Rozładuj gdy RCE > 600 PLN/MWh (wieczorna szczytna)

3. **PV to darmowa energia**
   - Zawsze preferuj PV nad import
   - Nigdy nie sprzedawaj PV żeby potem kupić z sieci (blokada przez `_calc_min_soc`)

4. **Obsługa ujemnych cen RCE**
   - Gdy RCE < 0 → ładuj baterię z sieci (`charge_battery` + import)
   - Nie sprzedawaj (exp = 0)

### Heurystyka okien cenowych

```
Dzień jest dzielony na 4 okna:
- before_min:  PV>=0.5 i przed minimum cenowym  → standby (jeśli pv>=3) lub auto
- cheap:       najniższe ceny (min + 150)       → auto (ładuj baterię)
- after_min:   po minimum, ceny rosną           → standardowa logika LP
- night:       brak PV (pv < 0.5)               → standardowa logika LP
```

---

## 5. Co należy jeszcze wdrożyć

### HIGH PRIORITY

#### [ ] MILP — rozwiązanie problemu cycling
- **Co:** Zastąp `linprog` na `scipy.optimize.milp` z binarną zmienną `bat_full[j]`
- **Efekt:** Poprawne SoC w planie dla wszystkich 36 slotów
- **Czas:** ~2-3 godziny implementacji
- **Zespół:** Ja mogę przygotować kod

#### [ ] Usunąć LP DEBUG logi
- **Co:** Zakomentować/usunąć linie z `self.log(f"LP DEBUG...")`
- **Gdzie:** `_solve_lp()` ~539-547
- **Czas:** 5 minut

#### [ ] Uczenie się z `ems_accuracy_view`
- **Co:** Korekta prognozy Solcast na podstawie historycznych błędów
- **Kiedy:** Od ~29-30 kwietnia (gdy będzie 14 dni danych porównania plan vs actual)
- **Efekt:** Dokładniejszy plan → lepsze decyzje
- **Status:** Czeka na historię

### MEDIUM PRIORITY

#### [ ] Integracja hydrofor / grzanie wody
- **Status:** Node-RED nodes przygotowane (`/mnt/user-data/outputs/hydrofor_nodes.json`)
- **Co brakuje:** Podłączenie w Node-RED + test
- **Czas:** 30 minut

#### [ ] System walidacji nadzorcy
- **Co:** Kompleksowa walidacja anomalii (cycling, błędne przejścia mode)
- **Status:** Wdrażane iteracyjnie w Node-RED
- **Następny krok:** Dodać do `debug_log` reguły heurystyczne

#### [ ] Analiza kosztów dziennych
- **Co:** Agregacja `ems_plan_log` → koszt/przychód per dzień
- **Efekt:** Monitorowanie efektywności systemu
- **Kiedy:** Po stabilizacji logiki LP

### LOW PRIORITY

#### [ ] Integracja EV ładowarki
- **Status:** Wyklucz z `_get_consumption()` gdy auto wróci
- **Czas:** ~30 minut (gdy EV będzie w systemie)

#### [ ] Dashboard HA
- **Co:** Wizualizacja planu + anomalii + kosztów
- **Narzędzie:** ApexCharts / custom card
- **Czas:** Później (po stabilizacji)

---

## 6. System testowania via GitHub

### Workflow: GitHub Actions

**Plik:** `.github/workflows/simulate.yml`

**Trigger:** 
- Każdy push do `ems_optimizer.py` lub `ems_simulator.py`
- Ręczny trigger (`workflow_dispatch`)

**Co się dzieje:**
1. Checkout kodu
2. Setup Python 3.11
3. `pip install scipy pytz`
4. `python ems_simulator.py`
5. Wydruk 7 scenariuszy testowych

**Scenariusze:**
```
1. Nocna 00:00 SoC=49% słonecznie       → test standardowy
2. Nocna 00:00 SoC=10% słonecznie       → test fallback (infeasible)
3. Poranna 06:00 SoC=16% ujemne ceny    → test taniej strefy
4. Wieczorna 18:00 SoC=100%             → test szczytowy
5. Poranna 06:00 SoC=10% słonecznie     → test fallback
6. Nocna 00:00 SoC=80% brak słońca      → test import (drogi dzień)
7. Południe 12:00 SoC=70% słonecznie    → test standardowy
```

**Logi zawierają:**
- `Godz` — godzina
- `Okno` — window pricing (before_min/cheap/after_min/night)
- `Tryb` — EMS mode
- `PV` — produkcja PV (kWh)
- `Cons` — konsumpcja (kWh)
- `Buy` — cena zakupu G13 (PLN/kWh)
- `RCE` — cena sprzedaży RCE (PLN/MWh)
- `Ch` — ładowanie baterii (kWh)
- `Dis` — rozładowanie baterii (kWh)
- `Imp` — import z sieci (kWh)
- `Exp` — eksport do sieci (kWh)
- `SoC` — stan baterii po slocie (%)
- `Min` — minimalne SoC wymagane (%)

**Walidacje:**
```
❌ BŁĄD: import w szczycie G13 o XX:00
❌ BŁĄD: sell_power przy PV < 2kWh o XX:00
```

**Wynik:**
- `✅ WSZYSTKIE SCENARIUSZE PRZESZŁY` → GitHub Actions zielone
- `❌ NIEKTÓRE SCENARIUSZE FAILED` → GitHub Actions czerwone (stop merge)

---

## 7. Workflow pracy dla kolejnych sesji

### Jeśli edytujesz kod:

1. **Edytuj** `ems_optimizer.py` na GitHub (przeglądarka) lub przez VS Code
2. **Commit** → push
3. **Czekaj** na GitHub Actions (zakładka Actions) — ~30 sekund
4. **Sprawdź** wydruk scenariuszy — czy `✅` czy `❌`
5. **Jeśli OK** → SSH do HA: `bash /config/apps/update_ems.sh`
6. **AppDaemon** przeładuje automatycznie (watch logi: `tail -f /config/appdaemon/logs/error.log`)

### Jeśli analizujesz logi symulacji:

1. Wklej surowy tekst z GitHub Actions
2. Kolumny `Buy` (cena G13) i `RCE` (cena sprzedaży) **nigdy nie mylisz**
3. Szukaj: flagi `⚠️` (import/ładowanie w szczycie)
4. Sprawdzaj: `Bilans:` (koszt - przychód)

---

## 8. Quick Reference — Komendy

### AppDaemon
```bash
# Restart na serwerze
ha addon restart a0d7b954_appdaemon

# Logi
tail -f /config/appdaemon/logs/main.log
tail -f /config/appdaemon/logs/error.log
```

### GitHub
```bash
# Wgraj zmianę optimizera
git add ems_optimizer.py
git commit -m "Opis zmian"
git push

# Automatycznie: GitHub Actions → symulacja
# Wynik: https://github.com/yakemishan/gw_ems_optimizer/actions
```

### Update HA
```bash
bash /config/apps/update_ems.sh
# Pobiera z GitHub + AppDaemon przeładowuje
```

---

## 9. Kontakt do poprzednich sesji

- **CONTEXT.md** — pełna dokumentacja systemu
- **Ostatnia sesja:** Okna cenowe + symulator importujący z optimizera
- **Log z GitHub Actions:** Do każdej sesji wklej surowy tekst loga dla analizy
