# EMS System — Dokumentacja dla Claude Code

> **Wersja:** 2.2.0  
> **Repozytorium:** `gw_ems_optimizer` (GitHub: yakemishan)  
> **Język:** Polski  
> **Ostatnia aktualizacja:** Kwiecień 2026

---

## 1. Architektura systemu

```
Solcast API → AppDaemon (EmsOptimizer) → sensor.ems_optimizer_decision
RCE PSE API →                          → sensor.ems_plan_text
MySQL hist. →                          → ems_plan_log (DB)

Node-RED (co 1 min) → select.inwerter_dom_ems_mode
                     → number.inwerter_dom_ems_power_limit
                     → switch.ciepla_woda_kotlownia_switch
                     → switch.hydrofor_piwnica_socket_1
                     → debug_log (DB)
```

## 2. Hardware

| Element | Wartość |
|---------|---------|
| Falownik | GoodWe z baterią |
| Pojemność baterii | **15.0 kWh** (uszkodzone ogniwa — efektywna pojemność) |
| Integracja HA | HACS mletenay (experimental) |
| Encja trybu | `select.inwerter_dom_ems_mode` |
| Encja limitu mocy | `number.inwerter_dom_ems_power_limit` |

## 3. Stałe systemu (ems_optimizer.py)

```python
BAT_CAPACITY         = 15.0   # kWh efektywne
BAT_MIN_SOC          = 0.10   # 10% minimum
BAT_MAX_SOC          = 1.00
BAT_MAX_CHARGE_KW    = 5.0
BAT_MAX_DISCHARGE_KW = 10.0
INVERTER_MAX_KW      = 15.0
EMS_VERSION          = "2.2.0"

# Ceny zakupu G13 (PLN/kWh) — TYLKO DO ZAKUPU
G13_SZCZYT_PRZED = 0.9047   # pon-pt 07-13
G13_SZCZYT_PO    = 1.4959   # pon-pt 19-22 (lato)
G13_POZOSTALE    = 0.6256   # wszystkie inne

# Ceny RCE PSE — TYLKO DO SPRZEDAŻY (nigdy do zakupu!)
```

## 4. Tryby GoodWe — zachowanie baterii

| Tryb | Bateria | PV | Import/Eksport |
|------|---------|-----|----------------|
| `auto` | Ładuje z nadwyżki PV; rozładowuje gdy brak PV | Pokrywa konsumpcję → bateria → eksport gdy 100% | Import gdy bat przy 10% i brak PV |
| `sell_power` | Rozładowuje do 10%, potem stoi | Eksportuje bezpośrednio | Eksport z PV + bat |
| `discharge_battery` | Rozładowuje stałą mocą (Xset=4500W) do celu | Eksportuje | Eksport z bat + PV |
| `battery_standby` | Stoi (PBat=0) | Eksportuje nadwyżkę bezpośrednio | Import z sieci gdy PV < konsumpcja |
| `charge_battery` | Ładuje z sieci (Xset=5000W) + PV nadwyżka | Pokrywa konsumpcję | Import z sieci |

## 5. Xset map (Node-RED)

```javascript
const xsetMap = {
    'sell_power':        20000,
    'discharge_battery': 4500,
    'charge_battery':    5000,
    'auto':              0,
    'battery_standby':   0,
}
```

## 6. Logika optymalizacji LP

### Cel
Minimalizuj: `Σ [buy_price_G13(t) · import(t) - sell_price_RCE(t) · export(t)]`

### Zasady nadrzędne
1. **Nie kupuj niepotrzebnie** — import tylko gdy niezbędny
2. **Nigdy nie kupuj żeby sprzedać** (anti-arbitraż)
3. **Import tylko w G13 Pozostałe** (0.6256 PLN/kWh) — nigdy w szczytach
4. **Sprzedawaj gdy RCE wysokie** i mamy nadwyżkę PV lub naładowaną baterię
5. **Przy ujemnych cenach RCE** → auto (nie sprzedajemy, ładujemy baterię)

### Okna cenowe (`_find_price_windows`)
Dzień jest dzielony na okna na podstawie cen RCE:

- **`before_min`** — PV jest, ceny jeszcze wysokie i malejące
  - PV >= 3 kWh → `battery_standby` (eksportuj bezpośrednio, bateria czeka na tańsze ładowanie)
  - PV < 3 kWh → `auto`
- **`cheap`** — najtańsze ceny (min_cena + 150 PLN/MWh)
  - Zawsze `auto` — ładuj baterię z PV
  - Obejmuje ujemne ceny
- **`after_min`** — ceny rosną po tanim oknie
  - Standardowa logika LP (sell/discharge/auto)
- **`night`** — brak PV
  - Standardowa logika LP

### `_calc_min_soc` — rezerwa nocna
- Sumuje zużycie tylko dla slotów **bez PV** (pv < 0.5 kWh)
- **NIE** wlicza szczytów G13 gdy PV produkuje (PV pokrywa szczyt)
- Ogranicza do `soc_init - cum_deficit` żeby LP nie był infeasible

### Fallback gdy LP infeasible
`_safe_auto_plan` — cały plan = `auto`, zero zakupów i sprzedaży

## 7. Node-RED — Nadzorca

**Interwał:** co 1 minutę

**Wejścia (7 join):**
- `inteligentne_sterowanie` (input_boolean)
- `stan_baterii` (sensor.battery_state_of_charge)
- `cena_energii` (sensor.rce_pse_cena)
- `produkcja_pv` (sensor.active_power)
- `ems_optimizer_decision` (przez extract_attrs_node)
- `current_inverter_mode` (select.inwerter_dom_ems_mode)
- `solcast_hour` (sensor.solcast_pv_forecast_moc_w_1_godzine)

**Priorytety:**
- P2: `inteligentne_sterowanie = off` → return null (ręczne sterowanie)
- P3+: logika nadzorcy

**Reguły per tryb:**

| Tryb planowany | Warunek interwencji | Akcja |
|---|---|---|
| `sell_power` | `solcastHour < 3000W` | → `auto` (nie loguj) |
| `sell_power` | `active_power < 0` przez 2 min | → `auto` + blokada do końca godziny |
| `discharge_battery` | `soc <= socAfterPct` (koniec bloku) | → `auto` + blokada do końca godziny |
| `auto` | `active_power < -500W` przez 2 min AND `plannedMode != 'charge_battery'` | → `auto` + log |
| `battery_standby` / `charge_battery` | `active_power < -500W` przez 2 min | → `auto` |

**`findBlockEndSoc`** — dla `discharge_battery` bierze `soc_after_pct` z końca całego bloku (nie bieżącego slotu)

**Logowanie do MySQL:**
- `shouldLog = overridden AND modeActuallyChanging AND isNewAnomaly`
- Tabela: `debug_log`

## 8. Baza danych MySQL

**Kluczowe metadata_id:**
| ID | Sensor |
|----|--------|
| 515 | sensor.inwerter_dom_dzienne_zuzycie |
| 133 | sensor.battery_state_of_charge (kolumna: `mean`) |
| 91  | sensor.today_s_pv_generation (dom) |
| 333 | sensor.today_s_pv_generation_2 (garaż) |
| 254 | sensor.ladowarka_licznik (EV, kumulatywny) |

**Tabele:**
- `ems_plan_log` — plan per sesja per slot
- `ems_accuracy_view` — VIEW: plan vs actual
- `debug_log` — logi nadzorcy

**WAŻNE:** `FROM_UNIXTIME()` zwraca CEST — NIE używaj `CONVERT_TZ()`

## 9. Sensory HA

| Sensor | Opis |
|--------|------|
| `sensor.ems_optimizer_decision` | Aktualny tryb + plan (atrybuty) |
| `sensor.ems_plan_text` | Plan jako tabela markdown |
| `sensor.solcast_pv_forecast_prognoza_na_dzisiaj` | PV dziś (detailedHourly) |
| `sensor.solcast_pv_forecast_prognoza_na_jutro` | PV jutro |
| `sensor.rce_pse_cena` | Aktualna cena RCE (PLN/MWh) — TYLKO SPRZEDAŻ |
| `sensor.rce_pse_cena_jutro` | Ceny RCE jutro (od ~13:00) |

## 10. Workflow deweloperski

```
1. Edytuj ems_optimizer.py na GitHub (przeglądarka)
2. Commit → GitHub Actions odpala ems_simulator.py automatycznie
3. Sprawdź wyniki w zakładce Actions
4. Jeśli OK → SSH do HA → bash /config/apps/update_ems.sh
5. AppDaemon wykrywa zmianę i przeładowuje automatycznie
```

## 11. Znane problemy / TODO

### Aktywne
- **Cycling w LP** — `ch` i `dis` jednocześnie niezerowe przy SoC=10% i dużym PV. Powoduje błędny SoC w wyświetlanym planie (sloty > 12h). Wymaga MILP lub przebudowy LP z SoC jako jawną zmienną. Sesje co 6h przeliczają z rzeczywistym SoC więc decyzje bieżące są poprawne.
- **LP DEBUG logi** — nadal w kodzie, do usunięcia przed produkcją

### Odłożone (za ~2 tygodnie, gdy będzie 14 dni danych)
- **System uczenia się** z `ems_accuracy_view` — korekta prognozy PV i zużycia

### Zrealizowane
- ✅ Sesje co 6h (00/06/12/18), horyzont 36h
- ✅ `_safe_auto_plan` jako fallback gdy LP infeasible (zamiast heurystyki)
- ✅ `_calc_min_soc` v2 — tylko is_no_pv, z ograniczeniem soc_init
- ✅ Okna cenowe `_find_price_windows` — before_min/cheap/after_min/night
- ✅ `can_sell` wymaga pv > 2.0 kWh i hour < 18
- ✅ `battery_standby` blokowane przy price <= 0
- ✅ Nadzorca: blokada sell_power, blokada discharge, anomalie
- ✅ Solcast < 3000W blokuje sell_power
- ✅ GitHub Actions + ems_simulator.py
- ✅ BAT_CAPACITY = 15.0 (uszkodzone ogniwa)

## 12. Pliki w repozytorium

| Plik | Opis |
|------|------|
| `ems_optimizer.py` | Główny kod AppDaemon |
| `ems_simulator.py` | Symulator standalone (bez HA) |
| `.github/workflows/simulate.yml` | GitHub Actions — auto-symulacja przy push |
