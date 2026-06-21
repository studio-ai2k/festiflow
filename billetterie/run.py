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

# DICE settlement gate (Bordeaux 2026) — the load-bearing reconciliation target.
DICE_GATE = {'tickets': 9329, 'gross_ttc': 624936.39, 'net_ht': 592356.77, 'vat': 32579.62}
# Shotgun acceptance floor (Bordeaux 2026).
SHOTGUN_GATE = {'tickets_sold': 17409, 'revenue_ht': 1171873.94}


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

    def line(label, got, target, tol):
        ok = abs(got - target) <= tol
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {got:,}  target {target:,}"
              + ('' if ok else f"  DELTA {got - target:+,.2f}"))
        return ok

    print("\n================ DICE SETTLEMENT GATE (Bordeaux 2026) ================")
    print(f"  (paid={paid}, free={free})")
    a = line("DICE tickets", n, DICE_GATE['tickets'], 0)
    # per-ticket rounding can drift a few cents vs the aggregate settlement; allow 5c on HT/VAT
    b = line("DICE gross TTC (no commission fold)", gross, DICE_GATE['gross_ttc'], 0.01)
    c = line("DICE net HT", ht, DICE_GATE['net_ht'], 0.05)
    e = line("DICE VAT", vat, DICE_GATE['vat'], 0.05)
    # anti-bug: gross must NOT equal the commission-folded 663,209.91
    folded = abs(gross - 663209.91) <= 1.0
    print(f"  [{'FAIL' if folded else 'PASS'}] gross is NOT the commission-folded 663,209.91"
          + (f"  (got {gross:,})" if folded else ''))
    gate_ok = a and b and c and e and not folded

    # capacity reconciliation
    print("\n  --- capacity reconciliation (DICE allocation vs config) ---")
    if dice_adapter is not None:
        print(f"  config total_capacity            = {cfg_cur['total_capacity']:,}")
        print(f"  DICE Event.totalTicketAllocationQty = {dice_adapter.total_allocation}")
        print(f"  DICE ticketPools                 = {dice_adapter.ticket_pools}")
        print(f"  DICE tickets totalCount (declared) = {dice_adapter.declared_total_count}")
        print(f"  DICE live fee categories         = {sorted(dice_adapter.fee_categories) or '(none returned)'}")
        if dice_adapter.undated:
            print(f"  DICE tickets with no claimedAt (dated to event start) = {dice_adapter.undated}")
        alloc = dice_adapter.total_allocation
        if alloc and alloc == cfg_cur['total_capacity']:
            print("  -> MATCH: API allocation == config capacity (could adopt API).")
        elif alloc:
            print("  -> MISMATCH (finding): keep config capacity; API allocation differs "
                  "(likely gross inventory vs marketing capacity).")
        else:
            print("  -> API allocation not returned; keep config capacity.")
    return gate_ok


def shotgun_gate_report(contract):
    by = contract['totals']['by_platform'].get('Shotgun', {})
    n = by.get('tickets_sold', 0)
    ht = by.get('net_ht', 0.0)
    print("\n================ SHOTGUN FLOOR (Bordeaux 2026) ================")
    ok1 = abs(n - SHOTGUN_GATE['tickets_sold']) <= 0
    ok2 = abs(round(ht) - round(SHOTGUN_GATE['revenue_ht'])) <= 2
    print(f"  [{'PASS' if ok1 else 'FAIL'}] Shotgun tickets_sold (paid): got {n:,}  target {SHOTGUN_GATE['tickets_sold']:,}")
    print(f"  [{'PASS' if ok2 else 'FAIL'}] Shotgun revenue_ht: got {ht:,.2f}  target {SHOTGUN_GATE['revenue_ht']:,.2f}")
    return ok1 and ok2


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

    # gates
    dice_ok = dice_gate_report(cur_tickets, dice_adapter, ev)
    sg_ok = shotgun_gate_report(contract)

    # annotate contract with the live capacity finding (non-destructive)
    if dice_adapter is not None:
        contract['capacity_source']['live_dice_allocation'] = dice_adapter.total_allocation
        contract['capacity_source']['live_dice_ticket_pools'] = dice_adapter.ticket_pools
        contract['capacity_source']['live_dice_fee_categories'] = sorted(dice_adapter.fee_categories)
        contract['capacity_source']['reconciliation'] = (
            'match' if dice_adapter.total_allocation == ev['total_capacity']
            else 'mismatch_keep_config')

    out = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   'output', ev.get('output_filename') or f"{args.event}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w') as f:
        json.dump(contract, f, indent=2, default=str)
    log(f"[run] wrote {out}: tickets_sold={contract['totals']['tickets_sold']} "
        f"revenue_ht={contract['totals']['revenue_ht']}")

    print("\n================ GATE SUMMARY ================")
    print(f"  DICE settlement gate : {'GREEN' if dice_ok else 'RED'}")
    print(f"  Shotgun floor        : {'GREEN' if sg_ok else 'RED'}")
    if not (dice_ok and sg_ok):
        print("  -> NOT SHIPPABLE: a gate is RED. Investigate before trusting the JSON.")
        sys.exit(1)
    print("  -> ALL GREEN: contract is settlement-reconciled.")


if __name__ == '__main__':
    main()
