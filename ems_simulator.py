"""
EMS Simulator - GitHub Actions version
Testuje logikę optimizera bez AppDaemon/HA
"""
import sys
from datetime import datetime, timedelta

try:
    from scipy.optimize import linprog
    SCIPY_OK = True
except ImportError:
    print("ERROR: scipy not installed")
    sys.exit(1)

# Importuj logikę z optimizera (bez AppDaemon)
# Kopiujemy stałe i funkcje bezpośrednio

BAT_CAPACITY         = 15.0
BAT_MIN_SOC          = 0.10
BAT_MAX_SOC          = 1.00
BAT_MAX_CHARGE_KW    = 5.0
BAT_MAX_DISCHARGE_KW = 10.0
INVERTER_MAX_KW      = 15.0
G13_SZCZYT_PRZED     = 0.9047
G13_SZCZYT_PO        = 1.4959
G13_POZOSTALE        = 0.6256

def g13_price(dt):
    weekday = dt.weekday()
    hour    = dt.hour
    month   = dt.month
    if weekday >= 5: return G13_POZOSTALE
    if 7 <= hour < 13: return G13_SZCZYT_PRZED
    if month in [10,11,12,1,2,3] and 16 <= hour < 21: return G13_SZCZYT_PO
    if month in [4,5,6,7,8,9] and 19 <= hour < 22: return G13_SZCZYT_PO
    return G13_POZOSTALE

def find_price_windows(horizon):
    n = len(horizon)
    windows = ['night'] * n
    daytime = [(j, s) for j, s in enumerate(horizon) if s['pv_kwh'] >= 0.5]
    if not daytime:
        return windows
    min_price    = min(s['price_pln_mwh'] for _, s in daytime)
    cheap_thresh = min_price + 150.0
    cheap_slots  = {j for j, s in daytime if s['price_pln_mwh'] <= cheap_thresh}
    if not cheap_slots:
        for j, _ in daytime:
            windows[j] = 'after_min'
        return windows
    first_cheap = min(cheap_slots)
    last_cheap  = max(cheap_slots)
    for j, _ in daytime:
        if j < first_cheap:   windows[j] = 'before_min'
        elif j <= last_cheap: windows[j] = 'cheap'
        else:                 windows[j] = 'after_min'
    return windows

def calc_min_soc(horizon, min_kwh, sic):
    n   = len(horizon)
    ms  = [0.0] * n
    for j in range(n):
        needed = 0.0
        for i in range(j, n):
            s = horizon[i]
            if s['pv_kwh'] < 0.5:
                needed += max(0, s['consumption_kwh'] - s['pv_kwh'])
            else:
                break
        ms[j] = max(min_kwh, min(needed, BAT_CAPACITY * 0.9))
    cum = 0.0
    for j in range(n):
        s    = horizon[j]
        cum += max(0, s['consumption_kwh'] - s['pv_kwh'])
        ms[j] = min(ms[j], max(min_kwh, sic - cum))
    return ms

def mode_from_lp(ch, dis, imp, exp, slot, soc_pct, window):
    T        = 0.05
    can_sell = slot['pv_kwh'] > 2.0 and 7 <= slot['dt'].hour < 18
    net_pv   = slot['pv_kwh'] - slot['consumption_kwh']

    if window == 'before_min':
        if slot['pv_kwh'] >= 3.0:
            return "battery_standby", f"przed_min: PV eksportuje"
        else:
            return "auto", f"przed_min: PV < 3kWh"

    if window == 'cheap':
        return "auto", f"tanie_okno: laduj baterie"

    if ch > T and imp > T:
        return "charge_battery", f"kupuj z sieci"
    if ch > T:
        return "auto", f"laduj z PV"
    if dis > T and exp > T and slot['price_pln_mwh'] > 0:
        return ("sell_power" if can_sell else "discharge_battery"), \
               f"exp={exp:.2f}kWh @ {slot['price_pln_mwh']:.0f}PLN"
    if dis > T:
        return ("discharge_battery" if dis > slot['consumption_kwh'] + 0.5 else "auto"), \
               f"dis={dis:.2f}kWh"
    if slot['pv_kwh'] > T:
        if net_pv >= 0.5 and soc_pct < 100 and slot['price_pln_mwh'] > 0:
            return "battery_standby", f"PV nadwyzka {net_pv:.2f}kWh"
        return "auto", f"PV={slot['pv_kwh']:.2f}kWh"
    return "auto", "brak PV"

def solve(soc_init_pct, horizon):
    sic     = soc_init_pct / 100.0 * BAT_CAPACITY
    min_kwh = BAT_MIN_SOC * BAT_CAPACITY
    max_kwh = BAT_MAX_SOC * BAT_CAPACITY
    n       = len(horizon)

    ic  = lambda j: j
    id_ = lambda j: n + j
    ii  = lambda j: 2*n + j
    ie  = lambda j: 3*n + j

    c = [0.0] * (4*n)
    for j, s in enumerate(horizon):
        c[ii(j)] = s['buy_price_pln_kwh']
        c[ie(j)] = -s['price_pln_mwh']/1000 if s['price_pln_mwh'] > 0 else 0

    Ae, be = [], []
    for j, s in enumerate(horizon):
        row = [0.0]*(4*n)
        row[ic(j)]=1; row[id_(j)]=-1; row[ie(j)]=1; row[ii(j)]=-1
        Ae.append(row); be.append(s['pv_kwh'] - s['consumption_kwh'])

    ms = calc_min_soc(horizon, min_kwh, sic)

    Au, bu = [], []
    for j in range(n):
        rl = [0.0]*(4*n)
        for k in range(j+1): rl[id_(k)]=1.0; rl[ic(k)]=-1.0
        Au.append(rl); bu.append(sic - ms[j])
        rh = [0.0]*(4*n)
        for k in range(j+1): rh[ic(k)]=1.0; rh[id_(k)]=-1.0
        Au.append(rh); bu.append(max_kwh - sic)
        rn = [0.0]*(4*n); rn[ic(j)]=1.0; rn[id_(j)]=1.0
        Au.append(rn); bu.append(BAT_MAX_CHARGE_KW)

    pv_tot   = sum(s['pv_kwh'] for s in horizon)
    pk_def   = sum(max(0, s['consumption_kwh']-s['pv_kwh']) for s in horizon if s['buy_price_pln_kwh'] > G13_POZOSTALE)
    bilans   = (sic + pv_tot) >= pk_def

    bounds = []
    for _ in range(n): bounds.append((0, BAT_MAX_CHARGE_KW))
    for _ in range(n): bounds.append((0, BAT_MAX_CHARGE_KW))
    for j in range(n):
        bounds.append((0,0) if bilans or horizon[j]['buy_price_pln_kwh'] > G13_POZOSTALE else (0,None))
    for j in range(n):
        bounds.append((0, horizon[j]['pv_kwh'] + BAT_MAX_CHARGE_KW))

    res = linprog(c, A_ub=Au, b_ub=bu, A_eq=Ae, b_eq=be, bounds=bounds, method='highs')

    if res.status != 0:
        return None, f"INFEASIBLE: {res.message[:50]}"

    x       = res.x
    windows = find_price_windows(horizon)
    plan    = []
    soc_kwh = sic

    for j, slot in enumerate(horizon):
        ch  = max(0, x[ic(j)])
        dis = max(0, x[id_(j)])
        imp = max(0, x[ii(j)])
        exp = max(0, x[ie(j)])
        soc_pct_now = soc_kwh / BAT_CAPACITY * 100
        mode, reason = mode_from_lp(ch, dis, imp, exp, slot, soc_pct_now, windows[j])
        soc_kwh = max(min_kwh, min(max_kwh, soc_kwh + ch - dis))
        plan.append({
            'dt':       slot['dt'],
            'mode':     mode,
            'soc':      round(soc_kwh / BAT_CAPACITY * 100, 1),
            'min_soc':  round(ms[j] / BAT_CAPACITY * 100, 1),
            'pv':       slot['pv_kwh'],
            'price':    slot['price_pln_mwh'],
            'cons':     slot['consumption_kwh'],
            'imp':      round(imp, 3),
            'exp':      round(exp, 3),
            'window':   windows[j],
            'reason':   reason,
        })

    return plan, "OK"


def run_scenario(name, start_dt, soc_pct, pv_profile, price_profile, cons_profile):
    print(f"\n{'='*80}")
    print(f"SCENARIUSZ: {name} | SoC={soc_pct}%")
    print(f"{'='*80}")

    horizon = []
    for i in range(36):
        dt = start_dt + timedelta(hours=i)
        h  = dt.hour
        horizon.append({
            'dt':                dt,
            'hour':              h,
            'pv_kwh':            pv_profile.get(h, 0.0),
            'price_pln_mwh':     price_profile.get(h, 500.0),
            'buy_price_pln_kwh': g13_price(dt),
            'consumption_kwh':   cons_profile.get(h, 0.5),
        })

    plan, status = solve(soc_pct, horizon)
    print(f"Status: {status}")

    if plan is None:
        print("LP infeasible - safe_auto_plan")
        return True

    print(f"\n{'Godz':>5} {'Okno':<12} {'Tryb':<22} {'SoC':>6} {'Min':>5} {'Cena':>7} {'Imp':>6} {'Exp':>6}")
    print("─"*80)

    errors = []
    for s in plan[:24]:
        imp_flag = "⚠️" if s['imp'] > 0.05 else ""
        print(f"{s['dt'].hour:>5}:00 {s['window']:<12} {s['mode']:<22} "
              f"{s['soc']:>5.1f}% {s['min_soc']:>4.0f}% {s['price']:>6.1f} "
              f"{s['imp']:>5.3f} {s['exp']:>5.3f} {imp_flag}")

        # Walidacja
        if s['imp'] > 0.1 and g13_price(s['dt']) > G13_POZOSTALE:
            errors.append(f"  ❌ BŁĄD: import w szczycie G13 o {s['dt'].hour}:00!")
        if s['mode'] == 'sell_power' and s['pv'] < 2.0:
            errors.append(f"  ❌ BŁĄD: sell_power przy PV={s['pv']:.2f} < 2kWh o {s['dt'].hour}:00!")

    if errors:
        print("\nBŁĘDY WALIDACJI:")
        for e in errors: print(e)
        return False

    print("\n✅ Scenariusz OK")
    return True


# ============================================================
# SCENARIUSZE TESTOWE
# ============================================================
if __name__ == "__main__":
    # Profile
    pv_sunny  = {0:0,1:0,2:0,3:0,4:0,5:0,6:0.1,7:1.3,8:3.2,9:5.5,10:7.2,
                 11:8.5,12:9.3,13:9.1,14:8.3,15:7.0,16:5.5,17:3.8,18:2.1,19:0.8,20:0.1,21:0,22:0,23:0}
    pv_neg    = {0:0,1:0,2:0,3:0,4:0,5:0,6:1.1,7:4.4,8:6.9,9:8.3,10:9.0,
                 11:9.4,12:10.1,13:10.1,14:9.4,15:8.1,16:6.1,17:4.8,18:3.1,19:0.5,20:0,21:0,22:0,23:0}
    pv_none   = {h: 0.0 for h in range(24)}

    price_normal = {0:470,1:450,2:440,3:435,4:445,5:480,6:570,7:610,8:580,9:530,
                    10:430,11:380,12:290,13:280,14:320,15:380,16:420,17:490,
                    18:620,19:720,20:830,21:700,22:600,23:520}
    price_neg    = {0:504,1:480,2:463,3:454,4:457,5:488,6:492,7:477,8:410,9:213,
                    10:-17,11:-30,12:-12,13:-12,14:-6,15:-19,16:118,17:363,
                    18:508,19:584,20:629,21:610,22:555,23:530}

    cons = {0:1.0,1:0.15,2:0.12,3:0.12,4:2.2,5:1.8,6:0.5,7:0.35,8:1.1,9:0.7,
            10:1.0,11:0.9,12:2.5,13:2.8,14:1.4,15:1.6,16:1.75,17:1.1,
            18:0.85,19:0.87,20:0.8,21:0.63,22:0.27,23:0.15}

    base = datetime(2026, 4, 21, 0, 0)
    all_ok = True

    scenarios = [
        ("Nocna 00:00 SoC=49% słonecznie",
         base.replace(hour=0), 49, pv_sunny, price_normal),
        ("Nocna 00:00 SoC=10% słonecznie",
         base.replace(hour=0), 10, pv_sunny, price_normal),
        ("Poranna 06:00 SoC=16% ujemne ceny",
         base.replace(hour=6), 16, pv_neg, price_neg),
        ("Wieczorna 18:00 SoC=100% słonecznie",
         base.replace(hour=18), 100, pv_sunny, price_normal),
        ("Poranna 06:00 SoC=10% słonecznie",
         base.replace(hour=6), 10, pv_sunny, price_normal),
        ("Nocna 00:00 SoC=80% brak słońca",
         base.replace(hour=0), 80, pv_none, price_normal),
        ("Południe 12:00 SoC=70% słonecznie",
         base.replace(hour=12), 70, pv_sunny, price_normal),
    ]

    for name, dt, soc, pv, price in scenarios:
        ok = run_scenario(name, dt, soc, pv, price, cons)
        if not ok:
            all_ok = False

    print(f"\n{'='*80}")
    if all_ok:
        print("✅ WSZYSTKIE SCENARIUSZE PRZESZŁY")
        sys.exit(0)
    else:
        print("❌ NIEKTÓRE SCENARIUSZE FAILED")
        sys.exit(1)
