# EMS Optimizer - AppDaemon app
# Harmonogram: co 6h (00/06/12/18) + na żądanie (input_boolean.ems_force_replan)
# Horyzont: 36h
# Tryby: charge_battery, auto, sell_power, discharge_battery, battery_standby

import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime, timedelta
import pytz
import mysql.connector
import mysql.connector.pooling

try:
    from scipy.optimize import linprog
    SCIPY_OK = True
except ImportError:
    SCIPY_OK = False


# ----------------------------------------------
# STALE
# ----------------------------------------------
BAT_CAPACITY         = 15.0   # kWh (uszkodzone ogniwa - efektywna pojemność)
EMS_VERSION          = "2.2.1"  # Bump: post-processing cycling
BAT_MIN_SOC          = 0.10
BAT_MAX_SOC          = 1.00
BAT_MAX_CHARGE_KW    = 5.0   # max moc ładowania (EMS power limit)
BAT_MAX_DISCHARGE_KW = 10.0  # max moc rozładowania baterii
INVERTER_MAX_KW      = 15.0  # max moc falownika

SOLCAST_TODAY    = "sensor.solcast_pv_forecast_prognoza_na_dzisiaj"
SOLCAST_TOMORROW = "sensor.solcast_pv_forecast_prognoza_na_jutro"
RCE_TODAY        = "sensor.rce_pse_cena"
RCE_TOMORROW     = "sensor.rce_pse_cena_jutro"
SOC_ENTITY       = "sensor.battery_state_of_charge"
FORCE_REPLAN     = "input_boolean.ems_force_replan"

CEST = pytz.timezone("Europe/Warsaw")

# Ceny zakupu G13 (PLN/kWh)
G13_SZCZYT_PRZED = 0.9047
G13_SZCZYT_PO    = 1.4959
G13_POZOSTALE    = 0.6256

# Godziny sesji planowania (co 6h)
PLAN_HOURS = {0, 6, 12, 18}


def g13_price(dt: datetime) -> float:
    weekday = dt.weekday()
    hour    = dt.hour
    month   = dt.month
    if weekday >= 5:
        return G13_POZOSTALE
    if 7 <= hour < 13:
        return G13_SZCZYT_PRZED
    if month in [10, 11, 12, 1, 2, 3] and 16 <= hour < 21:
        return G13_SZCZYT_PO
    if month in [4, 5, 6, 7, 8, 9] and 19 <= hour < 22:
        return G13_SZCZYT_PO
    return G13_POZOSTALE


class EmsOptimizer(hass.Hass):

    def initialize(self):
        self.log(f"EMS Optimizer v{EMS_VERSION} - start (horyzont 36h, sesje co 6h)")

        # -- Konfiguracja z apps.yaml --------------------------------------
        self._mysql_cfg = {
            "host":     self.args.get("mysql_host", "core-mariadb"),
            "port":     int(self.args.get("mysql_port", 3306)),
            "database": self.args.get("mysql_db", "homeassistant"),
            "user":     self.args.get("mysql_user", "homeassistant"),
            "password": self.args["mysql_pass"],
        }
        self._meta_consumption = int(self.args.get("metadata_id_consumption", 515))
        self._meta_soc         = int(self.args.get("metadata_id_soc", 133))
        self._meta_pv_home     = int(self.args.get("metadata_id_pv_home", 91))
        self._meta_pv_garage   = int(self.args.get("metadata_id_pv_garage", 333))

        # -- Connection pool MySQL -----------------------------------------
        try:
            self._db_pool = mysql.connector.pooling.MySQLConnectionPool(
                pool_name="ems_pool",
                pool_size=3,
                connection_timeout=10,
                **self._mysql_cfg,
            )
            self.log("MySQL connection pool - OK")
        except Exception as e:
            self._db_pool = None
            self.log(f"MySQL pool error (fallback: bezposrednie polaczenia): {e}", level="WARNING")

        if not SCIPY_OK:
            self.log("UWAGA: scipy niedostepne - LP wylaczone", level="WARNING")

        # -- Stan wewnetrzny -----------------------------------------------
        self.last_plan_slot: dict | None = None
        self._current_session_id: str | None = None

        # -- Harmonogram: co 6h (00/06/12/18) ------------------------------
        now_cest   = datetime.now(CEST)
        next_start = self._next_plan_hour(now_cest)
        delay_secs = (next_start - now_cest).total_seconds()

        # Uruchom natychmiast przy starcie
        self.run_in(self.optimize, 1)

        # Następnie co 6h o pełnych godzinach (00/06/12/18)
        self.run_every(
            self.optimize,
            next_start,
            6 * 3600,
        )

        # -- Nasłuch na force_replan ---------------------------------------
        self.listen_state(
            self._on_force_replan,
            FORCE_REPLAN,
            new="on",
        )

        self.log(
            f"Następna sesja planowania: {next_start.strftime('%Y-%m-%d %H:%M')} CEST "
            f"(za {delay_secs/60:.0f} min)"
        )

    def _next_plan_hour(self, now_cest: datetime) -> datetime:
        """Zwraca najbliższą godzinę sesji planowania (00/06/12/18) w przyszłości."""
        candidate = now_cest.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        while candidate.hour not in PLAN_HOURS:
            candidate += timedelta(hours=1)
        return candidate

    def _on_force_replan(self, entity, attribute, old, new, kwargs):
        """Wywoływany przez Node-RED gdy potrzebny jest replan."""
        self.log("Force replan otrzymany od Node-RED - uruchamiam optimize()")
        self.run_in(self.optimize, 2)
        # Resetuj flagę po chwili
        self.run_in(self._reset_force_replan, 10)

    def _reset_force_replan(self, kwargs):
        self.call_service("input_boolean/turn_off", entity_id=FORCE_REPLAN)
        self.log("Force replan - flaga zresetowana")

    # ------------------------------------------
    # POMOCNIK: polaczenie z baza
    # ------------------------------------------
    def _get_connection(self):
        if self._db_pool:
            return self._db_pool.get_connection()
        return mysql.connector.connect(connection_timeout=10, **self._mysql_cfg)

    # ------------------------------------------
    # GLOWNA PETLA
    # ------------------------------------------
    def optimize(self, kwargs):
        now_cest = datetime.now(CEST)
        self.log(f"-- Sesja planowania {now_cest.strftime('%Y-%m-%d %H:%M')} CEST --")

        # session_id: unikalny identyfikator sesji, np. "2026-04-14_06"
        session_id = now_cest.strftime("%Y-%m-%d_%H")
        self._current_session_id = session_id

        try:
            soc_pct      = self._get_soc()
            pv_slots     = self._get_pv_36h(now_cest)
            prices_slots = self._get_rce_36h(now_cest)
            consumption  = self._get_consumption()

            if not pv_slots or not prices_slots:
                self.log("Brak danych PV lub cen - pomijam", level="WARNING")
                return

            self.log(f"SoC: {soc_pct:.1f}%  |  Bateria: {soc_pct/100*BAT_CAPACITY:.2f} kWh")

            horizon = self._build_horizon(now_cest, pv_slots, prices_slots, consumption)
            self.log(f"Horyzont: {len(horizon)} slotów od {horizon[0]['dt'].strftime('%d/%m %H:%M')} "
                     f"do {horizon[-1]['dt'].strftime('%d/%m %H:%M')}")

            if SCIPY_OK and len(horizon) >= 2:
                plan = self._solve_lp(soc_pct / 100.0, horizon)
            else:
                plan = self._heuristic(soc_pct / 100.0, horizon)

            # Zapis całego planu do bazy
            self._save_plan_to_db(plan, session_id, now_cest, soc_pct)

            self._log_plan(plan, now_cest)
            self._log_current_decision(plan, now_cest, session_id)

        except Exception as e:
            self.log(f"Blad optymalizacji: {e}", level="ERROR")
            import traceback
            self.log(traceback.format_exc(), level="ERROR")

    # ------------------------------------------
    # DANE WEJSCIOWE - rozszerzone do 36h
    # ------------------------------------------
    def _get_soc(self) -> float:
        state = self.get_state(SOC_ENTITY)
        try:
            return float(state)
        except (TypeError, ValueError):
            self.log(f"Nieprawidlowy SoC: {state} - zakladam 50%", level="WARNING")
            return 50.0

    def _parse_solcast_hourly(self, entity: str) -> dict:
        attrs = self.get_state(entity, attribute="all")
        if not attrs:
            return {}
        detailed = attrs.get("attributes", {}).get("detailedHourly", [])
        result = {}
        for entry in detailed:
            try:
                ps = entry.get("period_start", "")
                dt = (
                    datetime.fromisoformat(ps).astimezone(CEST)
                    if isinstance(ps, str)
                    else ps.astimezone(CEST)
                )
                key = dt.replace(minute=0, second=0, microsecond=0)
                result[key] = float(entry.get("pv_estimate", 0))
            except Exception:
                continue
        return result

    def _get_pv_36h(self, now_cest: datetime) -> dict:
        """Łączy PV dziś i jutro — pokrywa 36h horyzontu."""
        today    = self._parse_solcast_hourly(SOLCAST_TODAY)
        tomorrow = self._parse_solcast_hourly(SOLCAST_TOMORROW)
        merged   = {**today, **tomorrow}
        if not merged:
            self.log("Brak danych Solcast", level="WARNING")
        return merged

    def _parse_rce_prices(self, entity: str) -> dict:
        attrs = self.get_state(entity, attribute="all")
        if not attrs:
            return {}
        prices_raw = attrs.get("attributes", {}).get("prices", [])
        buckets: dict[datetime, list] = {}
        for entry in prices_raw:
            try:
                dtime_str = entry.get("dtime", "")
                dt  = CEST.localize(datetime.strptime(dtime_str, "%Y-%m-%d %H:%M:%S"))
                key = dt.replace(minute=0, second=0, microsecond=0)
                buckets.setdefault(key, []).append(float(entry.get("rce_pln", 0)))
            except Exception:
                continue
        return {k: sum(v) / len(v) for k, v in buckets.items()}

    def _get_rce_36h(self, now_cest: datetime) -> dict:
        """Łączy ceny RCE dziś i jutro. Fallback: jutro = dziś+1d."""
        today    = self._parse_rce_prices(RCE_TODAY)
        tomorrow = self._parse_rce_prices(RCE_TOMORROW)
        if not tomorrow:
            self.log("Brak cen RCE na jutro - uzywam dzisiejszych jako przyblizenie", level="WARNING")
            tomorrow = {k + timedelta(days=1): v for k, v in today.items()}
        return {**today, **tomorrow}

    def _get_consumption(self) -> dict:
        """
        Średnie zużycie godzinowe z MySQL (14 dni).
        FROM_UNIXTIME() zwraca czas lokalny (CEST) - nie stosować CONVERT_TZ().
        """
        query = """
        SELECT
            HOUR(FROM_UNIXTIME(start_ts)) AS godzina,
            AVG(przyrost) AS avg_kwh
        FROM (
            SELECT
                start_ts,
                state - LAG(state) OVER (ORDER BY start_ts) AS przyrost
            FROM statistics
            WHERE metadata_id = %s
              AND start_ts >= UNIX_TIMESTAMP(NOW() - INTERVAL 14 DAY)
              AND start_ts < UNIX_TIMESTAMP(CURDATE())
        ) sub
        WHERE przyrost IS NOT NULL
          AND przyrost >= 0
          AND przyrost < 5
        GROUP BY godzina
        ORDER BY godzina
        """
        try:
            conn   = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(query, (self._meta_consumption,))
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            return {int(row[0]): float(row[1]) for row in rows if row[1] is not None}
        except Exception as e:
            self.log(f"MySQL _get_consumption error: {e}", level="WARNING")
            return {h: 1.0 for h in range(24)}

    # ------------------------------------------
    # BUDOWANIE HORYZONTU 36H
    # ------------------------------------------
    def _build_horizon(
        self,
        now_cest:     datetime,
        pv_slots:     dict,
        prices_slots: dict,
        consumption:  dict,
    ) -> list[dict]:
        """
        Buduje listę slotów godzinowych od teraz do teraz + 36 godzin.
        """
        start       = now_cest.replace(minute=0, second=0, microsecond=0)
        total_hours = 36

        slots_raw = []
        for i in range(total_hours):
            slot_dt = start + timedelta(hours=i)
            slots_raw.append({
                "dt":                slot_dt,
                "hour":              slot_dt.hour,
                "day":               slot_dt.strftime("%d/%m"),
                "pv_kwh":            pv_slots.get(slot_dt, 0.0),
                "price_pln_mwh":     prices_slots.get(slot_dt, 0.0),
                "buy_price_pln_kwh": g13_price(slot_dt),
                "consumption_kwh":   consumption.get(slot_dt.hour, 1.0),
            })

        # remaining_pv: suma PV od bieżącego slotu do końca tego samego dnia
        days = {}
        for s in slots_raw:
            days.setdefault(s["day"], []).append(s)

        horizon = []
        for s in slots_raw:
            day_slots        = days[s["day"]]
            future_pv        = sum(x["pv_kwh"] for x in day_slots if x["dt"] >= s["dt"])
            s["remaining_pv"] = round(future_pv, 3)
            horizon.append(s)

        return horizon

    # ------------------------------------------
    # ZAPIS PLANU DO BAZY
    # ------------------------------------------
    def _save_plan_to_db(
        self,
        plan:       list[dict],
        session_id: str,
        created_at: datetime,
        soc_pct:    float,
    ):
        """
        Zapisuje cały plan do tabeli ems_plan_log.
        Używa INSERT ... ON DUPLICATE KEY UPDATE żeby nadpisać stary plan
        dla tej samej sesji (np. przy force_replan w tej samej godzinie).
        """
        query = """
        INSERT INTO ems_plan_log (
            session_id, slot_dt, created_at,
            plan_mode, plan_soc_start_pct, plan_soc_end_pct,
            plan_pv_kwh, plan_cons_kwh,
            plan_import_kwh, plan_export_kwh,
            plan_price_pln_mwh, plan_buy_price_pln,
            plan_cost_pln, plan_min_soc_pct
        ) VALUES (
            %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s,
            %s, %s,
            %s, %s
        )
        ON DUPLICATE KEY UPDATE
            created_at         = VALUES(created_at),
            plan_mode          = VALUES(plan_mode),
            plan_soc_start_pct = VALUES(plan_soc_start_pct),
            plan_soc_end_pct   = VALUES(plan_soc_end_pct),
            plan_pv_kwh        = VALUES(plan_pv_kwh),
            plan_cons_kwh      = VALUES(plan_cons_kwh),
            plan_import_kwh    = VALUES(plan_import_kwh),
            plan_export_kwh    = VALUES(plan_export_kwh),
            plan_price_pln_mwh = VALUES(plan_price_pln_mwh),
            plan_buy_price_pln = VALUES(plan_buy_price_pln),
            plan_cost_pln      = VALUES(plan_cost_pln),
            plan_min_soc_pct   = VALUES(plan_min_soc_pct)
        """
        try:
            conn   = self._get_connection()
            cursor = conn.cursor()

            # Oblicz soc_start dla każdego slotu (soc_end poprzedniego = soc_start następnego)
            rows = []
            prev_soc = soc_pct
            for slot in plan:
                sell_kwh = slot.get("grid_export_kwh", 0.0) * slot.get("price", 0.0) / 1000.0
                buy_kwh  = slot.get("grid_import_kwh", 0.0) * slot["buy_price_pln_kwh"]
                cost     = round(buy_kwh - sell_kwh, 4)

                rows.append((
                    session_id,
                    slot["dt"].strftime("%Y-%m-%d %H:%M:%S"),
                    created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    slot["mode"],
                    round(prev_soc, 1),
                    round(slot.get("soc_after_pct", prev_soc), 1),
                    round(slot.get("pv_kwh", 0.0), 3),
                    round(slot.get("consumption_kwh", 0.0), 3),
                    round(slot.get("grid_import_kwh", 0.0), 3),
                    round(slot.get("grid_export_kwh", 0.0), 3),
                    round(slot.get("price", 0.0), 2),
                    round(slot.get("buy_price_pln_kwh",
                          slot["dt"] and g13_price(slot["dt"])), 4),
                    cost,
                    round(slot.get("min_soc_pct", 10.0), 1),
                ))
                prev_soc = slot.get("soc_after_pct", prev_soc)

            cursor.executemany(query, rows)
            conn.commit()
            cursor.close()
            conn.close()
            self.log(f"Zapisano {len(rows)} slotów planu do ems_plan_log (session={session_id})")

        except Exception as e:
            self.log(f"_save_plan_to_db error: {e}", level="ERROR")
            import traceback
            self.log(traceback.format_exc(), level="ERROR")

    # ------------------------------------------
    # POST-PROCESSING: Eliminacja cycling'u
    # ------------------------------------------
    def _post_process_cycling(self, x, idx_c, idx_d, n, horizon):
        """
        Jeśli LP zwróci cycling (ch > 0 i dis > 0 w tym samym slocie),
        usuń mniejszy z nich. Priorytet: ładowanie > rozładowanie.
        
        Logika:
        - Jeśli ch > threshold AND dis > threshold → dis = 0 (ładowanie wygrywa)
        - Zwraca poprawiony wektor x
        """
        THRESHOLD = 0.05
        x_fixed = list(x)
        cycling_slots = []
        
        for j in range(n):
            ch = max(0.0, x_fixed[idx_c(j)])
            dis = max(0.0, x_fixed[idx_d(j)])
            
            if ch > THRESHOLD and dis > THRESHOLD:
                # Cycling detected: zeruj rozładowanie (priorytet: ładowanie)
                x_fixed[idx_d(j)] = 0.0
                cycling_slots.append(j)
        
        if cycling_slots:
            self.log(f"Post-processing cycling: znaleziono {len(cycling_slots)} slotów "
                     f"z ch>0 i dis>0 → zerowanie dis (priorytet: ładowanie)")
        
        return x_fixed

    # ------------------------------------------
    # OPTYMALIZACJA LP
    # ------------------------------------------
    def _solve_lp(self, soc_init: float, horizon: list[dict]) -> list[dict]:
        n = len(horizon)

        idx_c = lambda h: h
        idx_d = lambda h: n + h
        idx_i = lambda h: 2 * n + h
        idx_e = lambda h: 3 * n + h
        total_vars = 4 * n

        c = [0.0] * total_vars
        for j, slot in enumerate(horizon):
            buy_kwh  = slot["buy_price_pln_kwh"]
            sell_kwh = slot["price_pln_mwh"] / 1000.0
            c[idx_i(j)] =  buy_kwh
            c[idx_e(j)] = -sell_kwh if sell_kwh > 0 else 0.0

        A_eq, b_eq = [], []
        for j, slot in enumerate(horizon):
            row = [0.0] * total_vars
            row[idx_c(j)] =  1.0
            row[idx_d(j)] = -1.0
            row[idx_e(j)] =  1.0
            row[idx_i(j)] = -1.0
            A_eq.append(row)
            b_eq.append(slot["pv_kwh"] - slot["consumption_kwh"])

        A_ub, b_ub = [], []
        soc_init_kwh = soc_init * BAT_CAPACITY
        min_kwh      = BAT_MIN_SOC * BAT_CAPACITY
        max_kwh      = BAT_MAX_SOC * BAT_CAPACITY

        min_soc_per_slot = self._calc_min_soc(horizon, min_kwh, soc_init_kwh)

        for j in range(n):
            row_lo = [0.0] * total_vars
            for k in range(j + 1):
                row_lo[idx_d(k)] =  1.0
                row_lo[idx_c(k)] = -1.0
            A_ub.append(row_lo)
            b_ub.append(soc_init_kwh - min_soc_per_slot[j])

            row_hi = [0.0] * total_vars
            for k in range(j + 1):
                row_hi[idx_c(k)] =  1.0
                row_hi[idx_d(k)] = -1.0
            A_ub.append(row_hi)
            b_ub.append(max_kwh - soc_init_kwh)

            # Blokada jednoczesnego ladowania i rozladowania (cycling)
            row_no_cycle = [0.0] * total_vars
            row_no_cycle[idx_c(j)] = 1.0
            row_no_cycle[idx_d(j)] = 1.0
            A_ub.append(row_no_cycle)
            b_ub.append(BAT_MAX_CHARGE_KW)

        total_peak_deficit = sum(
            max(0.0, s["consumption_kwh"] - s["pv_kwh"])
            for s in horizon
            if s["buy_price_pln_kwh"] > G13_POZOSTALE
        )
        pv_total       = sum(s["pv_kwh"] for s in horizon)
        bilans_dodatni = (soc_init_kwh + pv_total) >= total_peak_deficit

        if bilans_dodatni:
            self.log(
                f"Bilans dodatni (PV+bat={soc_init_kwh+pv_total:.1f} >= "
                f"deficyt={total_peak_deficit:.1f} kWh) - brak zakupow"
            )
        else:
            self.log(
                f"Bilans ujemny (deficyt Szczytow={total_peak_deficit:.1f} kWh) "
                f"- zakup dozwolony w Pozostale Godziny"
            )

        bounds = []
        for _ in range(n):
            bounds.append((0.0, BAT_MAX_CHARGE_KW))    # charge
        for _ in range(n):
            bounds.append((0.0, BAT_MAX_CHARGE_KW))    # discharge
        for j in range(n):
            if bilans_dodatni or horizon[j]["buy_price_pln_kwh"] > G13_POZOSTALE:
                bounds.append((0.0, 0.0))
            else:
                bounds.append((0.0, None))
        for j in range(n):
            bounds.append((0.0, horizon[j]["pv_kwh"] + BAT_MAX_CHARGE_KW))

        try:
            result = linprog(
                c, A_ub=A_ub, b_ub=b_ub,
                A_eq=A_eq, b_eq=b_eq,
                bounds=bounds, method="highs",
            )
        except Exception as e:
            self.log(f"linprog exception: {e} - fallback auto", level="WARNING")
            return self._safe_auto_plan(soc_init, horizon)

        if result.status != 0:
            self.log(f"LP status={result.status} ({result.message}) - fallback auto", level="WARNING")
            return self._safe_auto_plan(soc_init, horizon)

        x = result.x
        
        # ========== POST-PROCESSING: Eliminate cycling ==========
        x = self._post_process_cycling(x, idx_c, idx_d, n, horizon)
        # =========================================================

        plan    = []
        soc_kwh = soc_init_kwh

        # Oblicz okna cenowe dla całego horyzontu
        windows = self._find_price_windows(horizon)

        for j, slot in enumerate(horizon):
            ch  = max(0.0, x[idx_c(j)])
            dis = max(0.0, x[idx_d(j)])
            imp = max(0.0, x[idx_i(j)])
            exp = max(0.0, x[idx_e(j)])

            soc_pct_now = soc_kwh / BAT_CAPACITY * 100
            mode, reason = self._mode_from_lp(ch, dis, imp, exp, slot, soc_pct_now, windows[j])
            # SoC z LP (teraz poprawione dzięki post-processing cycling'u)
            soc_kwh = max(min_kwh, min(max_kwh, soc_kwh + ch - dis))

            plan.append({
                "dt":                slot["dt"],
                "hour":              slot["hour"],
                "day":               slot["day"],
                "mode":              mode,
                "reason":            reason,
                "bat_charge_kwh":    round(ch,  3),
                "bat_discharge_kwh": round(dis, 3),
                "grid_import_kwh":   round(imp, 3),
                "grid_export_kwh":   round(exp, 3),
                "soc_after_pct":     round(soc_kwh / BAT_CAPACITY * 100, 1),
                "min_soc_pct":       round(min_soc_per_slot[j] / BAT_CAPACITY * 100, 1),
                "remaining_pv":      round(slot.get("remaining_pv", 0.0), 2),
                "pv_kwh":            round(slot["pv_kwh"], 3),
                "price":             round(slot["price_pln_mwh"], 2),
                "buy_price_pln_kwh": round(slot["buy_price_pln_kwh"], 4),
                "consumption_kwh":   round(slot["consumption_kwh"], 3),
            })

        return plan

    # ------------------------------------------
    # MINIMALNE SOC PER SLOT
    # ------------------------------------------
    def _calc_min_soc(self, horizon: list[dict], min_kwh: float, soc_init_kwh: float = None) -> list[float]:
        """
        Dla każdego slotu oblicza minimalne SoC jakie musi zostać w baterii.

        Logika:
        1. Sumuj zużycie tylko dla slotów bez PV (pv < 0.5 kWh)
           - Szczyty G13 w dzień NIE są wliczane — gdy PV produkuje, pokrywa szczyt
        2. Ogranicz min_soc[j] do fizycznie osiągalnego przy nocnym rozładowaniu
           (soc_init - skumulowany deficyt 0..j), żeby LP nie był infeasible
        """
        n       = len(horizon)
        min_soc = [0.0] * n

        # Krok 1: oblicz rezerwę bazową (tylko sloty bez PV)
        for j in range(n):
            needed = 0.0
            for i in range(j, n):
                slot     = horizon[i]
                is_no_pv = slot["pv_kwh"] < 0.5
                deficit  = max(0.0, slot["consumption_kwh"] - slot["pv_kwh"])
                if is_no_pv:
                    needed += deficit
                else:
                    break
            min_soc[j] = max(min_kwh, min(needed, BAT_CAPACITY * 0.9))

        # Krok 2: ogranicz do fizycznie osiągalnego (zapobiega infeasible LP)
        if soc_init_kwh is not None:
            cum_deficit = 0.0
            for j in range(n):
                s           = horizon[j]
                cum_deficit += max(0.0, s["consumption_kwh"] - s["pv_kwh"])
                max_achievable = max(min_kwh, soc_init_kwh - cum_deficit)
                min_soc[j]  = min(min_soc[j], max_achievable)

        return min_soc

    # ------------------------------------------
    # MAPOWANIE LP -> TRYB GOODWE
    # ------------------------------------------
    def _find_price_windows(self, horizon: list[dict]) -> list[str]:
        """
        Identyfikuje okna cenowe dla każdego slotu:
        - 'before_min': PV jest, ceny wysokie/malejące przed tanim oknem → standby/auto
        - 'cheap':      najtańsze ceny → auto (ładuj baterię z PV)
        - 'after_min':  ceny rosną po tanim oknie → auto/sell/discharge
        - 'night':      brak PV
        """
        n = len(horizon)
        windows = ['night'] * n

        daytime = [(j, s) for j, s in enumerate(horizon) if s['pv_kwh'] >= 0.5]
        if not daytime:
            return windows

        min_price   = min(s['price_pln_mwh'] for _, s in daytime)
        cheap_thresh = min_price + 150.0

        cheap_slots = {j for j, s in daytime if s['price_pln_mwh'] <= cheap_thresh}
        if not cheap_slots:
            for j, _ in daytime:
                windows[j] = 'after_min'
            return windows

        first_cheap = min(cheap_slots)
        last_cheap  = max(cheap_slots)

        for j, _ in daytime:
            if j < first_cheap:
                windows[j] = 'before_min'
            elif j <= last_cheap:
                windows[j] = 'cheap'
            else:
                windows[j] = 'after_min'

        return windows

    def _mode_from_lp(
        self,
        ch: float, dis: float, imp: float, exp: float,
        slot: dict, soc_pct: float,
        window: str = 'night',
    ) -> tuple[str, str]:
        THRESHOLD = 0.05
        can_sell  = (slot["pv_kwh"] > 2.0 and 7 <= slot["dt"].hour < 18)
        net_pv    = slot["pv_kwh"] - slot["consumption_kwh"]

        # --------------------------------------------------------
        # LOGIKA OKIEN CENOWYCH (nadpisuje standardową logikę LP)
        # --------------------------------------------------------
        if window == 'before_min':
            if slot["pv_kwh"] >= 3.0:
                return (
                    "battery_standby",
                    f"przed_min: PV={slot['pv_kwh']:.2f}kWh eksportuje, "
                    f"bateria czeka na cena={slot['price_pln_mwh']:.0f} PLN/MWh",
                )
            else:
                return (
                    "auto",
                    f"przed_min: PV={slot['pv_kwh']:.2f}kWh < 3kWh, auto",
                )

        if window == 'cheap':
            return (
                "auto",
                f"tanie_okno: PV laduje baterie, cena={slot['price_pln_mwh']:.0f} PLN/MWh",
            )

        # --------------------------------------------------------
        # STANDARDOWA LOGIKA LP (after_min, night, brak PV)
        # --------------------------------------------------------
        if ch > THRESHOLD and imp > THRESHOLD:
            return (
                "charge_battery",
                f"kupuj {imp:.2f}kWh @ {slot['buy_price_pln_kwh']:.4f} PLN/kWh"
                f" (zabezpieczenie Szczytu)",
            )

        if ch > THRESHOLD:
            label = (
                f"PV nadwyzka {net_pv:.2f}kWh -> bateria"
                if net_pv >= 0
                else f"bateria pokrywa {-net_pv:.2f}kWh brakujacej energii"
            )
            return "auto", label

        if dis > THRESHOLD and exp > THRESHOLD and slot["price_pln_mwh"] > 0:
            if can_sell:
                return (
                    "sell_power",
                    f"agresywny eksport PV+bat: {exp:.2f}kWh"
                    f" @ {slot['price_pln_mwh']:.0f} PLN/MWh"
                    f" | PV={slot['pv_kwh']:.2f}kWh",
                )
            else:
                return (
                    "discharge_battery",
                    f"kontrolowane rozladowanie: {dis:.2f}kWh"
                    f" @ {slot['price_pln_mwh']:.0f} PLN/MWh"
                    f" | brak PV (godz. {slot['dt'].hour:02d}:00)",
                )

        if dis > THRESHOLD:
            if not can_sell and dis > slot["consumption_kwh"] + 0.5:
                return (
                    "discharge_battery",
                    f"kontrolowane rozladowanie: {dis:.2f}kWh"
                    f" @ {slot['price_pln_mwh']:.0f} PLN/MWh"
                    f" | brak PV (godz. {slot['dt'].hour:02d}:00)",
                )
            return "auto", f"bateria pokrywa {slot['consumption_kwh']:.2f}kWh brakujacej energii"

        if slot["pv_kwh"] > THRESHOLD:
            if net_pv >= 0.5 and soc_pct < 100 and slot["price_pln_mwh"] > 0:
                return (
                    "battery_standby",
                    f"PV={slot['pv_kwh']:.2f}kWh, nadwyzka {net_pv:.2f}kWh pokrywa dom"
                    f" | SoC={soc_pct:.0f}% - bateria odpoczywa",
                )
            return (
                "auto",
                f"PV={slot['pv_kwh']:.2f}kWh, mala nadwyzka {net_pv:.2f}kWh - auto wyrownuje bilans",
            )

        return "auto", f"brak PV (godz. {slot['dt'].hour:02d}:00), bateria pokrywa zuzycie"

    # ------------------------------------------
    # BEZPIECZNY FALLBACK (gdy LP infeasible)
    # ------------------------------------------
    def _safe_auto_plan(self, soc_init: float, horizon: list[dict]) -> list[dict]:
        """
        Fallback gdy LP jest infeasible (np. SoC za niski).
        Zwraca plan z samym auto - GoodWe sam zarządza.
        Nigdy nie sprzedaje ani nie kupuje z sieci.
        """
        self.log("LP infeasible - używam bezpiecznego planu auto (brak zakupów i sprzedaży)", level="WARNING")
        plan    = []
        soc     = soc_init * BAT_CAPACITY
        min_kwh = BAT_MIN_SOC * BAT_CAPACITY
        max_kwh = BAT_MAX_SOC * BAT_CAPACITY

        for slot in horizon:
            pv  = slot["pv_kwh"]
            co  = slot["consumption_kwh"]
            net = pv - co
            if net >= 0:
                ch      = min(net, BAT_MAX_CHARGE_KW, max_kwh - soc)
                soc    += ch
            else:
                dis     = min(-net, BAT_MAX_DISCHARGE_KW, soc - min_kwh)
                soc    -= dis

            plan.append({
                "dt":                slot["dt"],
                "hour":              slot["hour"],
                "day":               slot["day"],
                "mode":              "auto",
                "reason":            f"fallback auto (LP infeasible) | PV={pv:.2f}kWh",
                "soc_after_pct":     round(soc / BAT_CAPACITY * 100, 1),
                "min_soc_pct":       round(min_kwh / BAT_CAPACITY * 100, 1),
                "remaining_pv":      slot.get("remaining_pv", 0.0),
                "pv_kwh":            round(pv, 3),
                "price":             round(slot["price_pln_mwh"], 2),
                "buy_price_pln_kwh": round(slot["buy_price_pln_kwh"], 4),
                "consumption_kwh":   round(co, 3),
                "bat_charge_kwh":    0.0,
                "bat_discharge_kwh": 0.0,
                "grid_import_kwh":   0.0,
                "grid_export_kwh":   0.0,
            })
        return plan

    # ------------------------------------------
    # HEURYSTYKA (fallback gdy brak scipy)
    # ------------------------------------------
    def _heuristic(self, soc_init: float, horizon: list[dict]) -> list[dict]:
        plan    = []
        soc     = soc_init * BAT_CAPACITY
        min_kwh = BAT_MIN_SOC * BAT_CAPACITY
        max_kwh = BAT_MAX_SOC * BAT_CAPACITY

        prices = [s["price_pln_mwh"] for s in horizon]
        if prices:
            ps          = sorted(prices)
            low_thresh  = ps[len(ps) // 4]
            high_thresh = ps[3 * len(ps) // 4]
        else:
            low_thresh, high_thresh = 0.0, 999.0

        for slot in horizon:
            p      = slot["price_pln_mwh"]
            pv     = slot["pv_kwh"]
            co     = slot["consumption_kwh"]
            net_pv = pv - co

            if p < 0:
                mode   = "auto"
                reason = f"cena ujemna {p:.1f} PLN/MWh"
            elif p >= high_thresh and soc > min_kwh + 0.5:
                mode   = "sell_power"
                reason = f"szczyt cenowy {p:.0f} PLN/MWh"
                soc    = max(min_kwh, soc - min(INVERTER_MAX_KW - pv, BAT_MAX_DISCHARGE_KW))
            elif (p <= low_thresh
                  and slot["buy_price_pln_kwh"] <= G13_POZOSTALE
                  and soc < max_kwh - 0.5):
                mode   = "charge_battery"
                reason = f"tania strefa G13 {slot['buy_price_pln_kwh']:.4f} PLN/kWh"
                soc    = min(max_kwh, soc + min(BAT_MAX_CHARGE_KW, net_pv + 2))
            elif net_pv > 0.5 and soc < max_kwh - 0.5:
                mode   = "auto"
                reason = f"PV pokrywa zuzycie + {net_pv:.2f}kWh nadwyzki do baterii"
                soc    = min(max_kwh, soc + net_pv)
            elif pv > co:
                mode   = "auto"
                reason = f"PV={pv:.2f}kWh, nadwyzka {pv-co:.2f}kWh eksportuje"
            else:
                mode   = "auto"
                reason = "bateria pokrywa zuzycie"

            plan.append({
                "dt":                slot["dt"],
                "hour":              slot["hour"],
                "day":               slot["day"],
                "mode":              mode,
                "reason":            reason,
                "soc_after_pct":     round(soc / BAT_CAPACITY * 100, 1),
                "min_soc_pct":       0.0,
                "remaining_pv":      slot.get("remaining_pv", 0.0),
                "pv_kwh":            round(pv, 3),
                "price":             round(p, 2),
                "buy_price_pln_kwh": round(slot["buy_price_pln_kwh"], 4),
                "consumption_kwh":   round(co, 3),
                "bat_charge_kwh":    0.0,
                "bat_discharge_kwh": 0.0,
                "grid_import_kwh":   0.0,
                "grid_export_kwh":   0.0,
            })
        return plan

    # ------------------------------------------
    # LOGOWANIE PLANU
    # ------------------------------------------
    def _log_plan(self, plan: list[dict], now_cest: datetime):
        sep = "=" * 115
        self.log(sep)
        self.log(f"PLAN EMS 36h od {now_cest.strftime('%Y-%m-%d %H:%M')} CEST")
        self.log(
            f"{'Data':>6} {'Godz':>5} {'Tryb':<20} "
            f"{'PV':>6} {'RemPV':>6} {'Konsum':>7} "
            f"{'Cena':>8} {'SoC%':>6} {'Min%':>5}  Powod"
        )
        self.log("-" * 115)
        for s in plan:
            self.log(
                f"{s['day']:>6} "
                f"{s['hour']:>5}:00  "
                f"{s['mode']:<20} "
                f"{s.get('pv_kwh',0):>5.2f}k "
                f"{s.get('remaining_pv',0):>5.1f}k "
                f"{s.get('consumption_kwh',0):>6.2f}k "
                f"{s.get('price',0):>7.1f}PLN "
                f"{s.get('soc_after_pct',0):>5.1f}% "
                f"{s.get('min_soc_pct',0):>4.0f}%  "
                f"{s.get('reason','')}"
            )
        self.log(sep)

    def _log_current_decision(self, plan: list[dict], now_cest: datetime, session_id: str):
        now_hour = now_cest.replace(minute=0, second=0, microsecond=0)
        for slot in plan:
            if slot["dt"] == now_hour:
                self.log(
                    f"DECISION {slot['hour']:02d}:00 - "
                    f"mode={slot['mode']}, "
                    f"PV={slot.get('pv_kwh',0):.2f}kWh, "
                    f"cena={slot.get('price',0):.1f} PLN/MWh, "
                    f"SoC po={slot.get('soc_after_pct',0):.1f}%"
                )
                plan_serializable = []
                for s in plan:
                    entry = {k: v for k, v in s.items() if k != "dt"}
                    entry["dt_str"] = s["dt"].strftime("%Y-%m-%d %H:%M")
                    plan_serializable.append(entry)

                self.set_state(
                    "sensor.ems_optimizer_decision",
                    state=slot["mode"],
                    attributes={
                        "friendly_name":      "EMS Optimizer",
                        "session_id":         session_id,
                        "pv_kwh":             slot.get("pv_kwh", 0),
                        "price_pln_mwh":      slot.get("price", 0),
                        "soc_after_pct":      slot.get("soc_after_pct", 0),
                        "soc_start_pct":      slot.get("plan_soc_start_pct", 0),
                        "min_soc_pct":        slot.get("min_soc_pct", 10),
                        "consumption_kwh":    slot.get("consumption_kwh", 0),
                        "bat_charge_kwh":     slot.get("bat_charge_kwh", 0),
                        "bat_discharge_kwh":  slot.get("bat_discharge_kwh", 0),
                        "grid_import_kwh":    slot.get("grid_import_kwh", 0),
                        "grid_export_kwh":    slot.get("grid_export_kwh", 0),
                        "plan":               plan_serializable,
                        "updated":            now_cest.strftime("%Y-%m-%d %H:%M"),
                    },
                )
                self.last_plan_slot = slot
                self._update_plan_text(plan_serializable, now_cest)
                return

        self.log(f"Brak slotu dla {now_hour.strftime('%Y-%m-%d %H:%M')} w planie")

    def _update_plan_text(self, plan: list[dict], now_cest: datetime):
        MODE_EMOJI = {
            "sell_power":        "💰",
            "discharge_battery": "🔋",
            "charge_battery":    "⚡",
            "auto":              "🔄",
            "battery_standby":   "💤",
        }
        lines = [
            f"### Plan EMS 36h - {now_cest.strftime('%d/%m %H:%M')}",
            "| Data | Godz | Tryb | PV | RemPV | Konsum | Cena | SoC% | Min% | Powod |",
            "|---:|---:|---|---:|---:|---:|---:|---:|---:|---|",
        ]
        for s in plan:
            tryb  = s.get("mode", "")
            ikona = MODE_EMOJI.get(tryb, "")
            lines.append(
                f"| {s.get('day','')} "
                f"| {s.get('hour',0):02d}:00 "
                f"| {ikona} {tryb} "
                f"| {s.get('pv_kwh',0):.1f} "
                f"| {s.get('remaining_pv',0):.1f} "
                f"| {s.get('consumption_kwh',0):.1f} "
                f"| {s.get('price',0):.0f} "
                f"| {s.get('soc_after_pct',0):.0f}% "
                f"| {s.get('min_soc_pct',0):.0f}% "
                f"| {s.get('reason','')} |"
            )
        self.set_state(
            "sensor.ems_plan_text",
            state=now_cest.strftime("%Y-%m-%d %H:%M"),
            attributes={
                "friendly_name": "EMS Plan (tabela)",
                "text":          "\n".join(lines),
            },
        )
