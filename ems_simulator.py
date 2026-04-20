"""
EMS Simulator - GitHub Actions version
Importuje logikę bezpośrednio z ems_optimizer.py (bez AppDaemon/HA)
"""
import sys
import types
from datetime import datetime, timedelta

# ============================================================
# MOCK AppDaemon i MySQL
# ============================================================
mock_hass = types.ModuleType("appdaemon.plugins.hass.hassapi")
class MockHass:
    pass
mock_hass.Hass = MockHass
sys.modules["appdaemon"]                       = types.ModuleType("appdaemon")
sys.modules["appdaemon.plugins"]               = types.ModuleType("appdaemon.plugins")
sys.modules["appdaemon.plugins.hass"]          = types.ModuleType("appdaemon.plugins.hass")
sys.modules["appdaemon.plugins.hass.hassapi"]  = mock_hass

mock_mysql     = types.ModuleType("mysql")
mock_connector = types.ModuleType("mysql.connector")
mock_pooling   = types.ModuleType("mysql.connector.pooling")
mock_connector.pooling = mock_pooling
mock_mysql.connector   = mock_connector
sys.modules["mysql"]                   = mock_mysql
sys.modules["mysql.connector"]         = mock_connector
sys.modules["mysql.connector.pooling"] = mock_pooling

from ems_optimizer import (
    BAT_CAPACITY, BAT_MIN_SOC, BAT_MAX_SOC,
    BAT_MAX_CHARGE_KW, BAT_MAX_DISCHARGE_KW, INVERTER_MAX_KW,
    G13_SZCZYT_PRZED, G13_SZCZYT_PO, G13_POZOSTALE,
    EMS_VERSION, g13_price, EmsOptimizer,
)

print(f"EMS Simulator — logika z ems_optimizer.py v{EMS_VERSION}")
print(f"BAT_CAPACITY={BAT_CAPACITY} kWh | MIN_SOC={BAT_MIN_SOC*100:.0f}%")


class SimOptimizer(EmsOptimizer):
    def __init__(self):
        pass
    def log(self, msg, level="INFO"):
        pass

_opt = SimOptimizer()


def build_horizon(start_dt, pv_profile, price_profile, cons_profile):
    horizon = []
    for i in range(36):
        dt = start_dt + timedelta(hours=i)
        h  = dt.hour
        horizon.append({
            "dt":                dt,
            "hour":              h,
            "day":               dt.strftime("%d/%m"),
            "pv_kwh":            pv_profile.get(h, 0.0),
            "price_pln_mwh":     price_profile.get(h, 500.0),
            "buy_price_pln_kwh": g13_price(dt),
            "consumption_kwh":   cons_profile.get(h, 0.5),
            "remaining_pv":      0.0,
        })
    days = {}
    for s in horizon:
        days.setdefault(s["day"], []).append(s)
    for s in horizon:
        s["remaining_pv"] = round(sum(
            x["pv_kwh"] for x in days[s["day"]] if x["dt"] >= s["dt"]
        ), 3)
    return horizon


def run_scenario(name, start_dt, soc_pct, pv_profile, price_profile, cons_profile):
    print(f"\n{'='*100}")
    print(f"SCENARIUSZ: {name} | SoC={soc_pct}%")
    print(f"{'='*100}")

    horizon = build_horizon(start_dt, pv_profile, price_profile, cons_profile)
    plan    = _opt._solve_lp(soc_pct / 100.0, horizon)

    if not plan:
        print("Brak planu!")
        return False

    is_fallback = plan[0].get("reason", "").startswith("fallback")
    print(f"Status: {'INFEASIBLE→safe_auto' if is_fallback else 'LP OK'}")

    windows = _opt._find_price_windows(horizon)
    win_map = {s["dt"]: windows[i] for i, s in enumerate(horizon)}

    print(f"\n{'Godz':>5} {'Okno':<12} {'Tryb':<22} "
          f"{'PV':>6} {'Cons':>6} {'Cena':>7} "
          f"{'Ch':>5} {'Dis':>5} {'Imp':>6} {'Exp':>6} "
          f"{'SoC':>6} {'Min':>5}")
    print("─"*105)

    errors = []
    for s in plan[:24]:
        ch   = s.get("bat_charge_kwh", 0)
        dis  = s.get("bat_discharge_kwh", 0)
        imp  = s.get("grid_import_kwh", 0)
        exp  = s.get("grid_export_kwh", 0)
        w    = win_map.get(s["dt"], "-")
        flag = "⚠️" if imp > 0.05 else ""

        print(f"{s['dt'].hour:>5}:00 {w:<12} {s['mode']:<22} "
              f"{s.get('pv_kwh',0):>6.2f} {s.get('consumption_kwh',0):>6.2f} "
              f"{s.get('price',0):>6.1f} "
              f"{ch:>5.2f} {dis:>5.2f} {imp:>6.3f} {exp:>6.3f} "
              f"{s.get('soc_after_pct',0):>5.1f}% {s.get('min_soc_pct',0):>4.0f}% {flag}")

        if imp > 0.1 and g13_price(s["dt"]) > G13_POZOSTALE:
            errors.append(f"  ❌ import w szczycie G13 o {s['dt'].hour}:00!")
        if s["mode"] == "sell_power" and s.get("pv_kwh", 0) < 2.0:
            errors.append(f"  ❌ sell_power przy PV={s.get('pv_kwh',0):.2f} < 2kWh o {s['dt'].hour}:00!")

    total_imp = sum(s.get("grid_import_kwh", 0) for s in plan[:24])
    total_exp = sum(s.get("grid_export_kwh", 0) for s in plan[:24])
    cost      = sum(s.get("grid_import_kwh", 0) * s.get("buy_price_pln_kwh", G13_POZOSTALE) for s in plan[:24])
    revenue   = sum(s.get("grid_export_kwh", 0) * max(0, s.get("price", 0)) / 1000 for s in plan[:24])
    print(f"\n  Import: {total_imp:.2f} kWh  Koszt: {cost:.2f} PLN")
    print(f"  Export: {total_exp:.2f} kWh  Przychód: {revenue:.2f} PLN")
    print(f"  Bilans: {revenue - cost:+.2f} PLN")

    if errors:
        print("\nBŁĘDY WALIDACJI:")
        for e in errors: print(e)
        return False

    print("✅ OK")
    return True


if __name__ == "__main__":
    pv_sunny = {0:0,1:0,2:0,3:0,4:0,5:0,6:0.1,7:1.3,8:3.2,9:5.5,10:7.2,
                11:8.5,12:9.3,13:9.1,14:8.3,15:7.0,16:5.5,17:3.8,18:2.1,19:0.8,20:0.1,21:0,22:0,23:0}
    pv_neg   = {0:0,1:0,2:0,3:0,4:0,5:0,6:1.1,7:4.4,8:6.9,9:8.3,10:9.0,
                11:9.4,12:10.1,13:10.1,14:9.4,15:8.1,16:6.1,17:4.8,18:3.1,19:0.5,20:0,21:0,22:0,23:0}
    pv_none  = {h: 0.0 for h in range(24)}

    price_normal = {0:470,1:450,2:440,3:435,4:445,5:480,6:570,7:610,8:580,9:530,
                    10:430,11:380,12:290,13:280,14:320,15:380,16:420,17:490,
                    18:620,19:720,20:830,21:700,22:600,23:520}
    price_neg    = {0:504,1:480,2:463,3:454,4:457,5:488,6:492,7:477,8:410,9:213,
                    10:-17,11:-30,12:-12,13:-12,14:-6,15:-19,16:118,17:363,
                    18:508,19:584,20:629,21:610,22:555,23:530}
    cons = {0:1.0,1:0.15,2:0.12,3:0.12,4:2.2,5:1.8,6:0.5,7:0.35,8:1.1,9:0.7,
            10:1.0,11:0.9,12:2.5,13:2.8,14:1.4,15:1.6,16:1.75,17:1.1,
            18:0.85,19:0.87,20:0.8,21:0.63,22:0.27,23:0.15}

    base   = datetime(2026, 4, 21, 0, 0)
    all_ok = True

    scenarios = [
        ("Nocna 00:00 SoC=49% slonecznie",    base.replace(hour=0),  49,  pv_sunny, price_normal),
        ("Nocna 00:00 SoC=10% slonecznie",    base.replace(hour=0),  10,  pv_sunny, price_normal),
        ("Poranna 06:00 SoC=16% ujemne ceny", base.replace(hour=6),  16,  pv_neg,   price_neg),
        ("Wieczorna 18:00 SoC=100%",          base.replace(hour=18), 100, pv_sunny, price_normal),
        ("Poranna 06:00 SoC=10% slonecznie",  base.replace(hour=6),  10,  pv_sunny, price_normal),
        ("Nocna 00:00 SoC=80% brak slonca",   base.replace(hour=0),  80,  pv_none,  price_normal),
        ("Poludnie 12:00 SoC=70% slonecznie", base.replace(hour=12), 70,  pv_sunny, price_normal),
    ]

    for name, dt, soc, pv, price in scenarios:
        ok = run_scenario(name, dt, soc, pv, price, cons)
        if not ok:
            all_ok = False

    print(f"\n{'='*100}")
    if all_ok:
        print("✅ WSZYSTKIE SCENARIUSZE PRZESZLY")
        sys.exit(0)
    else:
        print("❌ NIEKTORE SCENARIUSZE FAILED")
        sys.exit(1)
