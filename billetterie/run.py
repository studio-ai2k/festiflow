#!/usr/bin/env python3
"""
run.py — LIVE orchestrator for the billetterie data layer.
==========================================================
Loads event_config.csv, builds live API clients from env tokens, fetches the
current + reference events, emits the JSON contract via the validated engine
(billetterie_datalayer.emit_contract), and runs the DICE settlement gate +
capacity reconciliation.

Modes:
  --mode recon   : probe live schema + each query path; no contract written.
  --mode fetch   : full live pull -> contract -> --out ; prints the gate report.

Env (NEVER committed; Actions/Codespaces secrets only):
  SHOTGUN_TOKEN, SHOTGUN_ORGANIZER_ID (default 171835), DICE_TOKEN
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date, datetime

import billetterie_datalayer as dl
import live


DEFAULT_ORGANIZER_ID = '171835'

# DICE settlement SNAPSHOT (Bordeaux 2026) — historical proof-of-correctness for
# the money MODEL, NOT an ongoing to-the-cent target. Per CC_DECISIONS Q1 we ship
# LIVE-FINAL; the gate confirms the model (count>0, no commission fold, shape) and
# reports the snapshot delta informationally.
DICE_SNAPSHOT = {'tickets': 9329, 'gross_ttc': 624936.39, 'net_ht': 592356.77, 'vat': 32579.62}
# Shotgun settlement snapshot (historical reference only).
SHOTGUN_SNAPSHOT = {'tickets_sold': 17409, 'revenue_ht': 1171873.94}
# The commission-fold bug value gross must NOT equal (model guard).
DICE_FOLDED_BUG = 663209.91
# Historical-fetch proof target: stored bordeaux_2025 Shotgun CSV (final export).
HISTORICAL_2025_SHOTGUN = {'tickets_sold': 9482, 'revenue_ht': 711580.48}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------
# CONFIG LOADER (event_config.csv -> per-event cfg dict the engine expects)
# ----------------------------------------------------------------------------

def load_config(path):
    events = {}
    with open(path, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            eid = row['event_id']
            ev = events.get(eid)
            if ev is None:
                ev = events[eid] = {
                    'event_id': eid,
                    'event_name': row.get('event_name', ''),
                    'comparison_mode': (row.get('comparison_mode') or 'j_minus').strip() or 'j_minus',
                    'compare_to': (row.get('compare_to') or '').strip(),
                    'status': (row.get('status') or '').strip(),
                    'dice_event_id': (row.get('dice_mio_id') or '').strip(),
                    'shotgun_event_id': (row.get('shotgun_event_id') or '').strip(),
                    'output_filename': (row.get('output_filename') or '').strip(),
                    'days': [],
                }
            dd = (row.get('day_date') or '').strip()
            day_date = datetime.strptime(dd, '%Y-%m-%d').date() if dd else None
            cap = (row.get('day_capacity') or '').strip()
            ev['days'].append({
                'day_number': int(row.get('day_number') or 1),
                'day_name': row.get('day_name', ''),
                'day_date': day_date,
                'day_capacity': int(cap) if cap else 0,
            })
    for ev in events.values():
        ev['days'].sort(key=lambda d: d['day_number'])
        dates = [d['day_date'] for d in ev['days'] if d['day_date']]
        ev['event_date_first'] = min(dates) if dates else None
        ev['event_date_last'] = max(dates) if dates else None
        ev['total_capacity'] = sum(d['day_capacity'] for d in ev['days'])
    return events


# ----------------------------------------------------------------------------
# CLIENTS
# ----------------------------------------------------------------------------

def build_clients():
    sg_token = os.environ.get('SHOTGUN_TOKEN', '')
    dice_token = os.environ.get('DICE_TOKEN', '')
    org_id = os.environ.get('SHOTGUN_ORGANIZER_ID', DEFAULT_ORGANIZER_ID)
    sg = live.ShotgunHTTPClient(sg_token) if sg_token else None
    dc = live.DiceGraphQLClient(dice_token) if dice_token else None
    return sg, dc, org_id, bool(sg_token), bool(dice_token)


def fetch_event_tickets(ev, sg_client, dc_client, org_id):
    """Returns (tickets, dice_adapter_or_None) for one configured event."""
    tickets = []
    dice_adapter = None
    if sg_client and ev.get('shotgun_event_id'):
        tickets += live.LiveShotgunAdapter(sg_client, org_id, ev['shotgun_event_id'], ev['days']).fetch()
    if dc_client and ev.get('dice_event_id'):
        dice_adapter = live.LiveDiceAdapter(dc_client, ev['dice_event_id'], ev['days'])
        tickets += dice_adapter.fetch()
    return tickets, dice_adapter


# ----------------------------------------------------------------------------
# GATE REPORT
# ----------------------------------------------------------------------------

def dice_gate_report(cur_tickets, dice_adapter, cfg_cur):
    d = [t for t in cur_tickets if t.platform == 'DICE']
    n = len(d)
    gross = round(sum(t.gross_ttc for t in d), 2)
    ht = round(sum(t.net_ht for t in d), 2)
    vat = round(sum(t.vat for t in d), 2)
    paid = sum(1 for t in d if t.is_paid)
    free = n - paid

    print("\n================ DICE MODEL GATE + settlement reference (Bordeaux 2026) ================")
    print(f"  live: tickets={n:,} (paid={paid}, free={free})  gross_ttc={gross:,.2f}  "
          f"net_ht={ht:,.2f}  vat={vat:,.2f}")
    delta = gross - DICE_SNAPSHOT['gross_ttc']
    pct = (gross / DICE_SNAPSHOT['gross_ttc'] - 1) * 100 if DICE_SNAPSHOT['gross_ttc'] else 0.0
    print(f"  settlement snapshot (historical proof): tickets={DICE_SNAPSHOT['tickets']:,} "
          f"gross={DICE_SNAPSHOT['gross_ttc']:,.2f} -> live delta {delta:+,.2f} ({pct:+.3f}%)")
    # MODEL checks (ship-critical): real data + commission NOT folded into gross.
    has_data = n > 0 and gross > 0
    folded = abs(gross - DICE_FOLDED_BUG) <= 1.0
    print(f"  [{'PASS' if has_data else 'FAIL'}] DICE returned live data (tickets>0, gross>0)")
    print(f"  [{'PASS' if not folded else 'FAIL'}] gross is NOT the commission-folded {DICE_FOLDED_BUG:,.2f}")
    model_ok = has_data and not folded

    if dice_adapter is not None:
        os_ = dice_adapter.orders_seen
        npa = dice_adapter.null_purchased_at
        print(f"\n  --- DICE fetch health (event-scoped viewer.orders) ---")
        print(f"  orders scanned={os_:,}  returns excluded={dice_adapter.returned_excluded}  "
              f"null face={dice_adapter.null_face}")
        print(f"  salesChannels={sorted(dice_adapter.sales_channels)}  "
              f"fee_categories={sorted(dice_adapter.fee_categories)}")
        if os_ and npa == os_:
            print(f"  [WARN] purchasedAt NULL on ALL {os_} orders -> token scope too low for dates "
                  f"(verify MIO restricted-access scope).")
        elif npa:
            print(f"  [WARN] purchasedAt null on {npa}/{os_} orders (those dated to event start).")
        else:
            print(f"  [PASS] purchasedAt populated on all {os_} orders (token scope OK).")

        print(f"\n  --- capacity reconciliation (DICE allocation vs config) ---")
        alloc = dice_adapter.total_allocation
        print(f"  config total_capacity={cfg_cur['total_capacity']:,} | "
              f"DICE totalTicketAllocationQty={alloc} | tickets totalCount={dice_adapter.declared_total_count}")
        if alloc and alloc == cfg_cur['total_capacity']:
            print("  -> MATCH (could adopt API allocation).")
        elif alloc:
            print("  -> MISMATCH (finding): keep config capacity (DICE allocation is its own "
                  "inventory, not the festival marketing capacity).")
        else:
            print("  -> API allocation not returned; keep config capacity.")
    return model_ok


def shotgun_report(contract):
    by = contract['totals']['by_platform'].get('Shotgun', {})
    n = by.get('tickets_sold', 0)
    ht = by.get('net_ht', 0.0)
    print("\n================ SHOTGUN (live-final) + settlement reference ================")
    print(f"  live: tickets_sold(paid)={n:,}  revenue_ht={ht:,.2f}")
    print(f"  settlement snapshot (historical): tickets_sold={SHOTGUN_SNAPSHOT['tickets_sold']:,} "
          f"revenue_ht={SHOTGUN_SNAPSHOT['revenue_ht']:,.2f} -> live delta "
          f"{n - SHOTGUN_SNAPSHOT['tickets_sold']:+,} tickets / {ht - SHOTGUN_SNAPSHOT['revenue_ht']:+,.2f} EUR "
          f"(live=final, snapshot=historical)")
    return n > 0


def historical_proof(ref_tickets):
    """V1 milestone: prove live historical fetch reconciles to the stored 2025
    Shotgun CSV (a final export, so live should match closely)."""
    print("\n================ HISTORICAL-FETCH PROOF (bordeaux_2025 live vs stored CSV) ================")
    if not ref_tickets:
        print("  [SKIP] no reference tickets fetched")
        return None
    sg = [t for t in ref_tickets if t.platform == 'Shotgun' and t.is_paid]
    n = len(sg)
    ht = round(sum(t.net_ht for t in sg), 2)
    tgt = HISTORICAL_2025_SHOTGUN
    # 2025 is a fully-settled, year-old event: live MUST reconcile to the stored
    # final CSV tightly. A gap here is a real fetch/filter/dating bug, NOT benign
    # snapshot-vs-live drift (that excuse only applies to the still-evolving 2026).
    ok_n = abs(n - tgt['tickets_sold']) <= 2
    ok_ht = abs(ht - tgt['revenue_ht']) <= 100
    print(f"  [{'PASS' if ok_n else 'FAIL'}] 2025 Shotgun tickets_sold: live {n:,}  stored {tgt['tickets_sold']:,}"
          + ('' if ok_n else f"  DELTA {n - tgt['tickets_sold']:+,}"))
    print(f"  [{'PASS' if ok_ht else 'FAIL'}] 2025 Shotgun revenue_ht: live {ht:,.2f}  stored {tgt['revenue_ht']:,.2f}"
          + ('' if ok_ht else f"  DELTA {ht - tgt['revenue_ht']:+,.2f}"))
    return ok_n and ok_ht


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Billetterie live pipeline")
    ap.add_argument('--mode', choices=['recon', 'fetch'], default='fetch')
    ap.add_argument('--event', default='bordeaux_2026')
    ap.add_argument('--config', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'event_config.csv'))
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    cfg_all = load_config(args.config)
    ev = cfg_all.get(args.event)
    if not ev:
        log(f"event {args.event} not in config"); sys.exit(2)

    sg, dc, org_id, has_sg, has_dice = build_clients()
    log(f"[run] mode={args.mode} event={args.event} shotgun_token={'yes' if has_sg else 'NO'} "
        f"dice_token={'yes' if has_dice else 'NO'} organizer_id={org_id}")

    if args.mode == 'recon':
        if not (has_sg or has_dice):
            log("recon needs at least one token in env"); sys.exit(2)
        if has_sg and ev.get('shotgun_event_id'):
            live.recon_shotgun(sg, org_id, ev['shotgun_event_id'])
        if has_dice and ev.get('dice_event_id'):
            live.recon_dice(dc, ev['dice_event_id'])
        log("[run] recon complete")
        return

    # ---- fetch ----
    if not (has_sg and has_dice):
        log("fetch needs BOTH SHOTGUN_TOKEN and DICE_TOKEN"); sys.exit(2)

    cur_tickets, dice_adapter = fetch_event_tickets(ev, sg, dc, org_id)
    log(f"[run] current '{args.event}': {len(cur_tickets)} tickets")

    ref_tickets, ref_cfg = None, None
    if ev.get('compare_to'):
        ref_cfg = cfg_all.get(ev['compare_to'])
        if ref_cfg:
            ref_tickets, _ = fetch_event_tickets(ref_cfg, sg, dc, org_id)
            log(f"[run] reference '{ev['compare_to']}': {len(ref_tickets)} tickets")

    contract = dl.emit_contract(cur_tickets, ev, ref_tickets, ref_cfg)

    # reports
    dice_ok = dice_gate_report(cur_tickets, dice_adapter, ev)   # model proven -> ship live-final
    sg_ok = shotgun_report(contract)
    hist_ok = historical_proof(ref_tickets)                     # V1 milestone (informational)

    # annotate contract with the live capacity finding (non-destructive)
    if dice_adapter is not None:
        contract['capacity_source']['live_dice_allocation'] = dice_adapter.total_allocation
        contract['capacity_source']['live_dice_ticket_pools'] = dice_adapter.ticket_pools
        contract['capacity_source']['live_dice_fee_categories'] = sorted(dice_adapter.fee_categories)
        contract['capacity_source']['reconciliation'] = (
            'match' if dice_adapter.total_allocation == ev['total_capacity']
            else 'mismatch_keep_config')

    # Stable, event-id-based filename so consumers have a constant URL.
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   'output', f"{args.event}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w') as f:
        json.dump(contract, f, indent=2, default=str)
    log(f"[run] wrote {out}: tickets_sold={contract['totals']['tickets_sold']} "
        f"revenue_ht={contract['totals']['revenue_ht']}")

    print("\n================ SUMMARY ================")
    print(f"  DICE model gate   : {'GREEN' if dice_ok else 'RED'}  (ship live-final; snapshot is historical proof)")
    print(f"  Shotgun live data : {'GREEN' if sg_ok else 'RED'}")
    print(f"  Historical proof  : {'GREEN' if hist_ok else ('RED' if hist_ok is False else 'N/A')} (2025 live vs stored CSV)")
    # SHIP gate: model proven + both platforms returned live data. Snapshot deltas
    # (live=final vs historical snapshot) are NOT ship-blocking per CC_DECISIONS Q1.
    if not (dice_ok and sg_ok):
        print("  -> NOT SHIPPABLE: model gate RED (no data or commission-fold). Investigate.")
        sys.exit(1)
    print("  -> SHIPPABLE: model proven, live-final contract written.")


if __name__ == '__main__':
    main()
