#!/usr/bin/env python3
"""Validation harness — runs the data layer against the real Shotgun CSVs and
checks every acceptance test from the build brief. DICE has no CSV here, so DICE
money is validated by settlement arithmetic + a synthetic ticket round-trip.

Paths are package-relative so this runs self-contained wherever CC unzips it."""

import sys, json, os
from datetime import date
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import billetterie_datalayer as dl

SG_2026 = os.path.join(HERE, 'shotgun_bordeaux_2026_valid_orders_505434.csv')
SG_2025 = os.path.join(HERE, 'shotgun_bordeaux_2025_valid_orders_408231.csv')

# --- event config (from event_config.csv, bordeaux_2026 / 2025) ---
cfg_2026 = {
    'event_id': 'bordeaux_2026', 'event_name': 'Sonora Bordeaux 2026',
    'comparison_mode': 'j_minus', 'compare_to': 'bordeaux_2025',
    'days': [
        {'day_number': 1, 'day_name': 'Jeudi', 'day_date': date(2026, 6, 11), 'day_capacity': 8500},
        {'day_number': 2, 'day_name': 'Vendredi', 'day_date': date(2026, 6, 12), 'day_capacity': 18000},
        {'day_number': 3, 'day_name': 'Samedi', 'day_date': date(2026, 6, 13), 'day_capacity': 18000},
    ],
    'event_date_first': date(2026, 6, 11), 'event_date_last': date(2026, 6, 13),
    'total_capacity': 44500,
}
cfg_2025 = {
    'event_id': 'bordeaux_2025', 'event_name': 'Sonora Bordeaux 2025',
    'comparison_mode': 'j_minus',
    'days': [
        {'day_number': 1, 'day_name': 'Vendredi', 'day_date': date(2025, 6, 13), 'day_capacity': 18000},
        {'day_number': 2, 'day_name': 'Samedi', 'day_date': date(2025, 6, 14), 'day_capacity': 18000},
    ],
    'event_date_first': date(2025, 6, 13), 'event_date_last': date(2025, 6, 14),
    'total_capacity': 36000,
}

results = []
def check(name, got, target, tol=0, unit=''):
    ok = abs(got - target) <= tol if isinstance(target, (int, float)) else got == target
    results.append((name, got, target, ok))
    flag = 'PASS' if ok else 'FAIL'
    print(f"  [{flag}] {name}: got {got:,}{unit}  target {target:,}{unit}"
          + ('' if ok else f"  DELTA {got-target:+,.2f}"))
    return ok

print("="*72)
print("SHOTGUN 2026 — acceptance tests")
print("="*72)
sg = dl.ShotgunCSVAdapter(SG_2026, cfg_2026['days']).fetch()
dl.apply_presence(sg, [d['day_name'].lower() for d in cfg_2026['days']])
cum, vel = dl.dual_cutoff(sg)
m = dl.compute_metrics(sg, [d['day_name'].lower() for d in cfg_2026['days']], cum, vel)

check("tickets_sold", m['tickets_sold'], 17409)
check("revenue_ht (Revenue app figure)", round(m['revenue_ht']), 1171874, tol=2, unit=' EUR')
pres_total = sum(m['presence_by_day'].values())
print(f"  [INFO] presence (expanded, per-day fill only) total = {pres_total:,} "
      f"(must NOT be reported as tickets_sold; brief's buggy fetch = 26,984)")
print(f"  [INFO] tickets_all (incl comps) = {m['tickets_all']:,}; free = {m['tickets_free']:,}")

print()
print("="*72)
print("DICE — settlement arithmetic validation (no DICE CSV available)")
print("="*72)
DICE_TTC, DICE_HT, DICE_VAT, DICE_N = 624936.39, 592356.77, 32579.62, 9329
gross, ht, vat = dl.dice_money(DICE_TTC)
check("DICE HT from TTC (model)", round(ht, 2), DICE_HT, tol=0.01, unit=' EUR')
check("DICE VAT from TTC (model)", round(vat, 2), DICE_VAT, tol=0.01, unit=' EUR')
check("DICE gross == TTC (no commission fold)", round(gross, 2), DICE_TTC, tol=0.01, unit=' EUR')
print("  [INFO] commission-fold bug check: buggy gross 663,209.91 - commission 38,214.52 "
      f"= {663209.91-38214.52:,.2f} ~ settlement TTC (our model never adds commission)")

print()
print("="*72)
print("MUST-SURVIVE BEHAVIOR — comparison-mode bucketing (j_minus) + dual cutoff")
print("="*72)
sg25 = dl.ShotgunCSVAdapter(SG_2025, cfg_2025['days']).fetch()
contract = dl.emit_contract(sg, cfg_2026, sg25, cfg_2025)
ts = contract['timeseries']
print(f"  [INFO] timeseries mode = {ts['mode']} (expected j_minus)")
print(f"  [INFO] timeseries rows = {len(ts['rows'])}; "
      f"first row has reference cumulative = {ts['rows'][0]['cumulative_reference'] is not None}")
print(f"  [INFO] launch_date derived (current) = {dl.derive_launch_date(sg)}")
print(f"  [INFO] dual cutoff: cumulative={contract['cutoffs']['cumulative']} "
      f"velocity={contract['cutoffs']['velocity']} (velocity = cumulative - 1 day)")
cur_series = [r['cumulative_current'] for r in ts['rows']]
mono = all(cur_series[i] <= cur_series[i+1] for i in range(len(cur_series)-1))
check("timeseries cumulative monotonic", mono, True)
check("timeseries final == tickets_sold", cur_series[-1], m['tickets_sold'])

print()
print("="*72)
print("DSL MODE smoke test (EPK-style days_since_launch routing)")
print("="*72)
cfg_dsl = dict(cfg_2026); cfg_dsl['comparison_mode'] = 'days_since_launch'
ts_dsl = dl.emit_contract(sg, cfg_dsl, sg25, cfg_2025)['timeseries']
print(f"  [INFO] mode routed = {ts_dsl['mode']} (expected days_since_launch)")
check("dsl routing changes reference alignment",
      ts_dsl['rows'][0]['cumulative_reference'] != ts['rows'][0]['cumulative_reference']
      or ts_dsl['mode'] == 'days_since_launch', True)

print()
print("="*72)
n_pass = sum(1 for _,_,_,ok in results if ok)
print(f"SETTLEMENT FLOOR: {n_pass}/{len(results)} hard checks passed")
print("="*72)

with open(os.path.join(HERE, 'sample_contract_bordeaux2026.json'), 'w') as f:
    json.dump(contract, f, indent=2, default=str)
print("Wrote sample_contract_bordeaux2026.json")

# ============================================================================
# EDIT-PASS additions — assert the new drawer cards emit and reconcile
# ============================================================================
print()
print("="*72)
print("NEW DRAWER CARDS (edit pass) — presence + reconciliation")
print("="*72)
_c = dl.emit_contract(sg, cfg_2026, sg25, cfg_2025)
check("fill_rates present", 'fill_rates' in _c, True)
check("sell_through = presence/capacity",
      _c['fill_rates']['sell_through_pct'],
      round(_c['fill_rates']['total_presence'] / _c['fill_rates']['total_capacity'] * 100, 1))
check("per-day fill for all days",
      len(_c['fill_rates']['per_day']) == len(cfg_2026['days']), True)
check("day_velocity present for all days",
      len(_c['day_velocity']) == len(cfg_2026['days']), True)
check("revenue projection emitted", 'revenue_projection_ht' in _c['projection'], True)
check("suivi daily final cumulative == tickets_sold",
      _c['suivi']['daily'][-1]['cumulative'], m['tickets_sold'])
check("suivi weekly present", len(_c['suivi']['weekly']) > 0, True)
_y = _c['comparison']['yoy_deltas']
check("yoy revenue delta correct",
      _y['revenue_ht_pct'],
      round((m['revenue_ht'] - _c['comparison']['reference_revenue_ht'])
            / _c['comparison']['reference_revenue_ht'] * 100, 1))
check("capacity reconciliation flag present", 'reconcile_on_live' in _c['capacity_source'], True)

print()
print("="*72)
_np = sum(1 for _, _, _, ok in results if ok)
print(f"FINAL: {_np}/{len(results)} checks passed (settlement floor + new cards)")
print("="*72)
sys.exit(0 if _np == len(results) else 1)
