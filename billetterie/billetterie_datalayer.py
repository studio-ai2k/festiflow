#!/usr/bin/env python3
"""
FESTIFLOW — Billetterie data layer (rewrite)
=============================================
Emits the JSON contract the V1 drawer + recette consume.

This REPLACES the standalone's input layer (CSV/zip read -> API fetch) and
output layer (HTML bake -> JSON emit). The two must-survive behaviors
(comparison-mode bucketing + dual cutoff) and the proven classification logic
are re-implemented here as *behavior*, validated against:
  (a) the standalone's outputs, and
  (b) the real settlement numbers (Bordeaux 2026).

MONEY MODEL (resolved from settlement docs — NOT the standalone's approximations):
  DICE   : gross = TTC (= ticket face). HT = TTC / 1.055. VAT = TTC - HT.
           Commission is BUYER-paid -> never folded into promoter gross.
  Shotgun: deal_price (cents) = HT = promoter net. Service/user fees are
           BUYER-paid on top (TTC) -> never deducted from promoter net.
           VAT is the 5.5% component embedded per the same rate.

COUNT MODEL (two distinct concepts, kept SEPARATE in the JSON):
  tickets_sold : valid/scanned rows, paid (price>0 / not comp), counted ONCE.
                 -> headline KPI + recette + settlement validation.
  presence     : each ticket expanded across EVERY day it grants access.
                 -> per-day fill-rate ONLY. Never summed as "tickets sold".

INPUT ADAPTERS are pluggable:
  - ShotgunCSVAdapter / DiceCSVAdapter : read the merged/export CSVs (offline,
    used for validation against ground truth here).
  - ShotgunAPIAdapter / DiceAPIAdapter : real API fetch (REST / GraphQL).
    The CSV and API adapters emit the IDENTICAL normalized per-ticket record,
    so the engine and JSON are source-agnostic.

DICE dating: Ticket.claimedAt is claim time. Date each ticket by its
  Order.purchasedAt (join tickets -> order). Shotgun ordered_at is correct.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict
from typing import Optional, Iterable


# ============================================================================
# CONSTANTS
# ============================================================================

ALL_DAYS = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche']

# VAT rate on the ticket face (festival rate, FR). Settlement confirms 5.5% for
# both platforms' face. Used to split TTC<->HT for DICE; Shotgun face is already HT.
VAT_RATE = 0.055

# Comparison modes
J_MINUS = 'j_minus'
DAYS_SINCE_LAUNCH = 'days_since_launch'


# ============================================================================
# NORMALIZED PER-TICKET RECORD  (the single shape every adapter must emit)
# ============================================================================

@dataclass
class Ticket:
    """One sold ticket, normalized. Source-agnostic (CSV or API produce this)."""
    order_date: date                 # purchase DATE (drives every curve)
    order_datetime: Optional[datetime]  # full purchase timestamp (cosmetic: last-sale)
    platform: str                    # 'DICE' | 'Shotgun'
    ticket_type: str                 # 'jeudi'|'vendredi'|'samedi'|'2-jours'|'3-jours'|'single_day'
    access_level: str                # regular|vip|backstage|early_entry|invitation|jeu_concours|group_discount
    attendance_days: Optional[list]  # concrete day names this ticket grants, or None
    product_name: str
    # MONEY — all three explicit, per ticket, settlement-honest (euros):
    gross_ttc: float                 # buyer-facing ticket face incl. VAT (promoter receives this for DICE; Shotgun HT*1+fees is separate)
    net_ht: float                    # promoter net of VAT  (the "Revenue"/recette figure)
    vat: float                       # VAT component
    is_paid: int                     # 1 paid, 0 comp/free

    # derived per-day presence filled by the engine (presence_<day> -> 0/1)
    presence: dict = field(default_factory=dict)


# ============================================================================
# CLASSIFICATION  (ported verbatim-in-behavior from the standalone's proven logic)
# ============================================================================

def classify_ticket(name, price=None, tags='', event_days=None):
    """Universal classifier — same behavior as the standalone.
    Returns (ticket_type, access_level, attendance_days, product_name)."""
    if not name:
        return 'single_day', 'regular', [], ''

    raw = name.strip()
    n = raw.upper()
    n_clean = n
    for suffix in [' - JOUR 1', ' - JOUR 2', ' - JOUR 3', ' - DAY 1', ' - DAY 2', ' - DAY 3',
                   '(DERNIERS TICKETS)', '(OFFRE ULTRA LIMITÉE)', '(OFFRE ULTRA LIMITEE)']:
        n_clean = n_clean.replace(suffix, '')
    n_clean = re.sub(
        r'\d{1,2}\s+(JANVIER|FEVRIER|FÉVRIER|MARS|AVRIL|MAI|JUIN|JUILLET|AOUT|AOÛT|'
        r'SEPTEMBRE|OCTOBRE|NOVEMBRE|DECEMBRE|DÉCEMBRE)', '', n_clean)

    # access level
    access_level = 'regular'
    if tags and tags.strip().lower() == 'invitation':
        access_level = 'invitation'
    elif 'INVITATION' in n:
        access_level = 'invitation'
    elif 'JEU CONCOURS' in n:
        access_level = 'jeu_concours'
    elif 'VIP' in n or 'ACCÈS SCÈNE' in n or 'ACCES SCENE' in n or 'GOLD' in n:
        access_level = 'vip'
    elif 'BACKSTAGE' in n and 'VIP' not in n:
        access_level = 'backstage'
    elif 'ENTRÉE AVANT' in n or 'ENTREE AVANT' in n:
        access_level = 'early_entry'
    elif '5 POUR 4' in n:
        access_level = 'group_discount'
    if price is not None and float(price) == 0 and access_level == 'regular':
        access_level = 'invitation'

    # days mentioned
    days_found = [d for d in ALL_DAYS if d.upper() in n_clean]
    paren = re.search(r'\(([^)]+)\)', n_clean)
    if paren:
        for d in ALL_DAYS:
            if d.upper() in paren.group(1) and d not in days_found:
                days_found.append(d)
    days_found.sort(key=lambda d: ALL_DAYS.index(d))

    # date-based fallback -> map to event day
    if not days_found and event_days:
        MONTHS = {'JANVIER':1,'FEVRIER':2,'FÉVRIER':2,'MARS':3,'AVRIL':4,'MAI':5,'JUIN':6,
                  'JUILLET':7,'AOUT':8,'AOÛT':8,'SEPTEMBRE':9,'OCTOBRE':10,'NOVEMBRE':11,
                  'DECEMBRE':12,'DÉCEMBRE':12,'JAN':1,'FEV':2,'FÉV':2,'MAR':3,'AVR':4,'JUN':6,
                  'JUL':7,'SEP':9,'OCT':10,'NOV':11,'DEC':12,'DÉC':12}
        m = re.search(r'(\d{1,2})\s+(' + '|'.join(MONTHS.keys()) + r')', n)
        if m:
            dnum = int(m.group(1)); mnum = MONTHS.get(m.group(2))
            for ed in event_days:
                dd = ed.get('day_date')
                if dd and dd.day == dnum and dd.month == mnum:
                    days_found.append(ed['day_name'].lower()); break

    # ticket type
    if '3 JOURS' in n_clean or 'TROIS JOURS' in n_clean:
        ticket_type = '3-jours'; attendance_days = days_found if len(days_found) >= 3 else None
    elif '2 JOURS' in n_clean or 'DEUX JOURS' in n_clean:
        ticket_type = '2-jours'; attendance_days = days_found if len(days_found) >= 2 else None
    elif '1 JOUR' in n_clean:
        ticket_type = 'single_day'; attendance_days = days_found if days_found else None
    elif len(days_found) >= 3:
        ticket_type = '3-jours'; attendance_days = days_found
    elif len(days_found) == 2:
        ticket_type = '2-jours'; attendance_days = days_found
    elif len(days_found) == 1:
        ticket_type = days_found[0]; attendance_days = days_found
    else:
        ticket_type = 'single_day'; attendance_days = None

    product_name = raw.strip()
    if product_name.isupper():
        product_name = product_name.title()
    return ticket_type, access_level, attendance_days, product_name


def normalize_dice_type(structured_name, face_ttc, event_days):
    """EDIT 1 — Structured-first DICE type resolution (robustness upgrade).

    DICE exposes a structured `ticketType.name`. Use it as the PRIMARY source,
    but normalize it INTO the standalone's canonical taxonomy so by-type
    breakdowns don't fragment (the audit guardrail). classify_ticket() is the
    FALLBACK only when the structured name is missing or doesn't normalize.

    Returns (ticket_type, access_level, attendance_days, product_name, source)
    where source ∈ {'structured', 'fallback'} for observability.
    The canonical taxonomy MUST match classify_ticket's output set:
      ticket_type ∈ {jeudi,vendredi,samedi,dimanche,2-jours,3-jours,single_day}
      access_level ∈ {regular,vip,backstage,early_entry,invitation,jeu_concours,group_discount}
    """
    name = (structured_name or '').strip()
    if not name:
        # no structured name -> fall back entirely to string parsing
        tt, access, att, pn = classify_ticket('', price=face_ttc, event_days=event_days)
        return tt, access, att, pn, 'fallback'

    # The structured DICE name is the authoritative label, but it is STILL a
    # human string ("PASS 3 JOURS (JEUDI...)"). We normalize it through the SAME
    # canonical classifier so the type taxonomy is identical to the standalone's
    # — i.e. we trust the structured field as the label source, and use the
    # proven normalizer to bucket it. This satisfies the guardrail (no parallel
    # naming scheme) while removing reliance on filename/Item-Type guesswork.
    tt, access, att, _ = classify_ticket(name, price=face_ttc, event_days=event_days)

    # product_name = the structured name verbatim (clean display label), not a
    # title-cased reconstruction.
    product_name = name.title() if name.isupper() else name

    # If the structured name failed to yield a concrete type (single_day with no
    # day resolved), that's the unmapped case — already handled by classify_ticket
    # returning single_day; mark source accordingly for observability.
    source = 'structured' if tt != 'single_day' or att else 'structured_unmapped'
    return tt, access, att, product_name, source


def resolve_attendance(ticket_type, attendance_days, event_day_names):
    """Expand a ticket across the days it grants access (presence model)."""
    presence = {dn: 0 for dn in event_day_names}
    if attendance_days:
        for d in attendance_days:
            if d in presence:
                presence[d] = 1
    elif ticket_type == '3-jours':
        for dn in event_day_names:
            presence[dn] = 1
    elif ticket_type == '2-jours':
        main = event_day_names[-2:] if len(event_day_names) >= 2 else event_day_names
        for dn in main:
            presence[dn] = 1
    elif ticket_type in event_day_names:
        presence[ticket_type] = 1
    elif ticket_type == 'single_day':
        for dn in event_day_names:   # conservative: count on all days
            presence[dn] = 1
    return presence


def determine_day_from_dates(date_debut):
    if not date_debut:
        return None
    try:
        d = date_debut.strip().split(' ')[0].replace('/', '-')
        wd = datetime.strptime(d, '%Y-%m-%d').weekday()
        return {0:'lundi',1:'mardi',2:'mercredi',3:'jeudi',4:'vendredi',5:'samedi',6:'dimanche'}[wd]
    except Exception:
        return None


# ============================================================================
# MONEY  (settlement-honest; the single source of truth for HT / VAT / TTC)
# ============================================================================

def dice_money(face_ttc: float):
    """DICE: promoter receives the full ticket TTC (commission is buyer-paid).
    HT = TTC / (1+vat); VAT = TTC - HT."""
    ht = round(face_ttc / (1 + VAT_RATE), 4)
    vat = round(face_ttc - ht, 4)
    return face_ttc, ht, vat


def shotgun_money(deal_price_ht: float):
    """Shotgun: deal_price (face, already HT to the promoter) IS the net.
    The buyer-paid service/user fees are on top (TTC) and are NOT promoter revenue.
    VAT is the 5.5% embedded in the face for reporting; the recette net = HT = deal_price."""
    ht = deal_price_ht
    # gross to buyer (TTC incl service fees) is reported separately when available;
    # for the recette/Revenue figure the promoter net is HT = deal_price.
    vat = round(ht - ht / (1 + VAT_RATE), 4)   # VAT embedded in the face, for the VAT line
    ttc_face = ht                               # promoter-facing gross == HT face (fees excluded)
    return ttc_face, ht, vat


# ============================================================================
# INPUT ADAPTERS
# ============================================================================

def _fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


class ShotgunCSVAdapter:
    """Reads a Shotgun valid_orders export CSV -> List[Ticket].
    Mirrors the live API mapping: PRICE(HT)≙deal_price, CLIENT PRICE≙deal_price+fees(TTC)."""

    def __init__(self, csv_path, event_days):
        self.csv_path = csv_path
        self.event_days = event_days
        self.event_day_names = [d['day_name'].lower() for d in event_days]

    def fetch(self) -> list[Ticket]:
        out = []
        with open(self.csv_path, encoding='utf-8') as f:
            for row in csv.DictReader(f):
                status = (row.get('STATUS') or row.get('STATUT') or '').strip()
                if status not in ('valid', 'scanned'):
                    continue
                raw_dt = row.get('PURCHASE DATE') or row.get('DATE ACHAT') or ''
                od = self._date(raw_dt)
                if not od:
                    continue
                odt = self._datetime(raw_dt)

                category = row.get('CATEGORY', row.get('CATEGORIE', '')) or ''
                deal_title = row.get('DEAL TITLE', row.get('NOM DU TARIF', '')) or ''
                tags = row.get('TAGS', '') or ''
                combined = f"{category} {deal_title}".strip()

                ht = _fnum(row.get('PRICE', row.get('PRIX HT', '')))   # promoter net (HT)
                client = _fnum(row.get('CLIENT PRICE', row.get('PRIX CLIENT', '')))  # buyer TTC

                tt, access, att_days, _ = classify_ticket(combined, price=client, tags=tags,
                                                           event_days=self.event_days)
                product_name = category.strip().title() if category.strip().isupper() else category.strip()

                if (not att_days) and tt == 'single_day':
                    d = determine_day_from_dates(row.get('START', row.get('DEBUT', '')))
                    if d:
                        tt = d; att_days = [d]

                is_paid = 1 if ht > 0 else 0
                # Shotgun money: deal_price (= HT) is promoter net; client(TTC) reported as gross.
                _, net_ht, vat = shotgun_money(ht)
                out.append(Ticket(
                    order_date=od, order_datetime=odt, platform='Shotgun',
                    ticket_type=tt, access_level=access, attendance_days=att_days,
                    product_name=product_name,
                    gross_ttc=client, net_ht=net_ht, vat=vat, is_paid=is_paid,
                ))
        return out

    @staticmethod
    def _date(s):
        if not s or not s.strip():
            return None
        try:
            p = s.strip().split(' ')[0].replace('/', '-')
            return datetime.strptime(p, '%Y-%m-%d').date()
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _datetime(s):
        if not s or not s.strip():
            return None
        c = s.strip().replace('/', '-')
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
            try:
                return datetime.strptime(c, fmt)
            except ValueError:
                pass
        return None


class ShotgunAPIAdapter:
    """Live Shotgun REST adapter. Same output shape as the CSV adapter.

    Field mapping (from shotgun_schema.json, all money in CENTS):
      ordered_at            -> order_date / order_datetime  (true purchase time)
      deal_sub_category     -> CATEGORY  (classification + product_name)
      deal_title            -> DEAL TITLE (classification secondary)
      deal_price (cents)/100-> HT (promoter net = recette Revenue)
      deal_price + deal_service_fee + deal_user_service_fee (cents)/100 -> TTC (buyer paid)
      deal_vat_rate         -> VAT rate (cross-check; face VAT computed at VAT_RATE)
      ticket_status=='valid'/'scanned' and ticket_canceled_at is None -> keep
    """

    def __init__(self, client, organizer_id, event_id, event_days):
        self.client = client          # caller injects an HTTP client (requests-like)
        self.organizer_id = organizer_id
        self.event_id = event_id
        self.event_days = event_days

    def fetch(self) -> list[Ticket]:
        out = []
        for t in self._iter_tickets():
            if t.get('ticket_status') not in ('valid', 'scanned'):
                continue
            if t.get('ticket_canceled_at'):
                continue
            odt = self._parse(t.get('ordered_at'))
            if not odt:
                continue
            category = t.get('deal_sub_category', '') or ''
            deal_title = t.get('deal_title', '') or ''
            combined = f"{category} {deal_title}".strip()

            ht = (t.get('deal_price') or 0) / 100.0
            fees = ((t.get('deal_service_fee') or 0) + (t.get('deal_user_service_fee') or 0)) / 100.0
            ttc = round(ht + fees, 2)

            tt, access, att_days, _ = classify_ticket(combined, price=ttc, tags='',
                                                       event_days=self.event_days)
            product_name = category.title() if category.isupper() else category
            is_paid = 1 if ht > 0 else 0
            _, net_ht, vat = shotgun_money(ht)
            out.append(Ticket(
                order_date=odt.date(), order_datetime=odt, platform='Shotgun',
                ticket_type=tt, access_level=access, attendance_days=att_days,
                product_name=product_name,
                gross_ttc=ttc, net_ht=net_ht, vat=vat, is_paid=is_paid,
            ))
        return out

    def _iter_tickets(self) -> Iterable[dict]:
        """Paginate api.shotgun.live/tickets via pagination.next."""
        url = "https://api.shotgun.live/tickets"
        params = {"organizer_id": self.organizer_id, "event_id": self.event_id,
                  "include_cohosted_events": False}
        while url:
            resp = self.client.get(url, params=params).json()
            for t in resp.get("tickets", []):
                yield t
            nxt = (resp.get("pagination") or {}).get("next")
            url, params = (nxt, None) if nxt else (None, None)

    @staticmethod
    def _parse(s):
        if not s:
            return None
        s = s.strip().replace('/', '-')
        for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%d %H:%M'):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                pass
        return None


class DiceAPIAdapter:
    """Live DICE GraphQL adapter. Dates each ticket by its Order.purchasedAt
    (NOT Ticket.claimedAt). Money per the settlement: face = TTC, HT = TTC/1.055.

    GraphQL shape used (from dice_schema.json):
      viewer { orders { edges { node {
        purchasedAt
        returns { ticketId }          # exclude returned tickets
        tickets { edges { node {
          id  fullPrice  total
          ticketType { name description }
          fees { category dice promoter }
        }}}
      }}}}

    Money: DICE 'fullPrice'/'total' (cents) is the buyer-facing face = TTC.
    Commission (ticket.commission/diceCommission) is BUYER-paid -> NOT added to gross.
    """

    def __init__(self, client, event_id, event_days):
        self.client = client
        self.event_id = event_id
        self.event_days = event_days

    def fetch(self) -> list[Ticket]:
        out = []
        for order in self._iter_orders():
            purchased = self._parse((order or {}).get('purchasedAt'))
            if not purchased:
                continue
            returned_ids = {r.get('ticketId') for r in (order.get('returns') or [])}
            for edge in ((order.get('tickets') or {}).get('edges') or []):
                node = edge.get('node') or {}
                if node.get('id') in returned_ids:
                    continue
                tt_obj = node.get('ticketType') or {}
                # EDIT 1: structured ticketType.name is the PRIMARY label source.
                structured_name = tt_obj.get('name') or ''

                face_cents = node.get('fullPrice')
                if face_cents is None:
                    face_cents = node.get('total') or 0
                face_ttc = face_cents / 100.0      # buyer face = promoter gross (commission buyer-paid)

                tt, access, att_days, product_name, _src = normalize_dice_type(
                    structured_name, face_ttc, self.event_days)
                is_paid = 0 if access in ('invitation', 'jeu_concours') or face_ttc == 0 else 1
                gross_ttc, net_ht, vat = dice_money(face_ttc)
                out.append(Ticket(
                    order_date=purchased.date(), order_datetime=purchased, platform='DICE',
                    ticket_type=tt, access_level=access, attendance_days=att_days,
                    product_name=product_name,
                    gross_ttc=gross_ttc, net_ht=net_ht, vat=vat, is_paid=is_paid,
                ))
        return out

    def _iter_orders(self) -> Iterable[dict]:
        """Paginate viewer.orders (cursor). The query is parameterized by event."""
        cursor = None
        while True:
            data = self.client.query(self._gql(), {"eventId": self.event_id, "after": cursor})
            conn = (((data or {}).get('viewer') or {}).get('orders') or {})
            for e in (conn.get('edges') or []):
                yield e.get('node') or {}
            pi = conn.get('pageInfo') or {}
            if pi.get('hasNextPage'):
                cursor = pi.get('endCursor')
            else:
                break

    @staticmethod
    def _gql():
        return """
        query Orders($eventId: ID!, $after: String) {
          viewer { orders(eventId: $eventId, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges { node {
              purchasedAt
              returns { ticketId }
              tickets { edges { node {
                id fullPrice total
                ticketType { name description }
                fees { category dice promoter }
              } } }
            } }
          } }
        }"""

    @staticmethod
    def _parse(s):
        if not s:
            return None
        s = str(s).strip()
        for fmt in ('%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                pass
        try:
            return datetime.fromisoformat(s.replace('Z', '+00:00')).replace(tzinfo=None)
        except ValueError:
            return None


class DiceCSVAdapter:
    """Reads a DICE doorlist/merged CSV (offline). Money: 'Price' is the buyer
    face (TTC); HT/VAT derived at VAT_RATE. Dating: 'Purchase date' (the merged
    CSV already carries true purchase date)."""

    def __init__(self, csv_path, event_days, price_col='Price', date_col='Purchase date',
                 type_col='Item Type'):
        self.csv_path = csv_path
        self.event_days = event_days
        self.price_col, self.date_col, self.type_col = price_col, date_col, type_col

    def fetch(self) -> list[Ticket]:
        out = []
        with open(self.csv_path, encoding='utf-8') as f:
            for row in csv.DictReader(f):
                od = ShotgunCSVAdapter._date(row.get(self.date_col, ''))
                if not od:
                    continue
                odt = ShotgunCSVAdapter._datetime(row.get(self.date_col, ''))
                face = _clean_price(row.get(self.price_col, '0'))
                label = (row.get(self.type_col, '') or '').strip()
                # EDIT 1: DICE 'Item Type' is the structured-name analogue -> structured-first.
                tt, access, att_days, product_name, _src = normalize_dice_type(
                    label, face, self.event_days)
                is_paid = 0 if access in ('invitation', 'jeu_concours') or face == 0 else 1
                gross_ttc, net_ht, vat = dice_money(face)
                out.append(Ticket(
                    order_date=od, order_datetime=odt, platform='DICE',
                    ticket_type=tt, access_level=access, attendance_days=att_days,
                    product_name=product_name,
                    gross_ttc=gross_ttc, net_ht=net_ht, vat=vat, is_paid=is_paid,
                ))
        return out


def _clean_price(s):
    if s is None:
        return 0.0
    s = str(s).replace('€', '').replace('$', '').replace(',', '.').strip()
    m = re.search(r'-?\d+(\.\d+)?', s)
    return float(m.group(0)) if m else 0.0


# ============================================================================
# ENGINE — presence, dual cutoff, metrics, comparison bucketing, velocity, projections
# ============================================================================

def apply_presence(tickets: list[Ticket], event_day_names: list[str]) -> None:
    """Fill ticket.presence with presence_<day> 0/1 across granted days."""
    for t in tickets:
        pres = resolve_attendance(t.ticket_type, t.attendance_days, event_day_names)
        t.presence = {f'presence_{dn}': pres.get(dn, 0) for dn in event_day_names}


def dual_cutoff(tickets: list[Ticket]):
    """cutoff_cumulative = max order_date (all data, partial today OK).
    cutoff_velocity   = max_date - 1 (last complete day) for rates/projections."""
    if not tickets:
        today = date.today()
        return today, today - timedelta(days=1)
    max_date = max(t.order_date for t in tickets)
    return max_date, max_date - timedelta(days=1)


def _paid(tickets):
    return [t for t in tickets if t.is_paid == 1]


def compute_metrics(tickets: list[Ticket], event_day_names: list[str],
                    cutoff_cumulative: date, cutoff_velocity: date) -> dict:
    """Headline metrics. tickets_sold = paid, counted once. presence kept separate."""
    cum = [t for t in tickets if t.order_date <= cutoff_cumulative]
    paid = _paid(cum)

    tickets_sold = len(paid)                 # headline KPI (settlement-validated)
    tickets_all = len(cum)                    # incl. comps
    free = tickets_all - tickets_sold

    revenue_ht = round(sum(t.net_ht for t in paid), 2)
    revenue_ttc = round(sum(t.gross_ttc for t in paid), 2)
    revenue_vat = round(sum(t.vat for t in paid), 2)

    by_platform = defaultdict(lambda: {'tickets_sold': 0, 'net_ht': 0.0, 'gross_ttc': 0.0, 'vat': 0.0})
    by_type = defaultdict(lambda: {'tickets_sold': 0, 'net_ht': 0.0})
    by_access = defaultdict(int)
    for t in cum:
        by_access[t.access_level] += 1
        if t.is_paid:
            p = by_platform[t.platform]
            p['tickets_sold'] += 1; p['net_ht'] += t.net_ht; p['gross_ttc'] += t.gross_ttc; p['vat'] += t.vat
            ty = by_type[t.ticket_type]
            ty['tickets_sold'] += 1; ty['net_ht'] += t.net_ht
    for p in by_platform.values():
        p['net_ht'] = round(p['net_ht'], 2); p['gross_ttc'] = round(p['gross_ttc'], 2); p['vat'] = round(p['vat'], 2)
    for ty in by_type.values():
        ty['net_ht'] = round(ty['net_ht'], 2)

    # presence (attendance) — SEPARATE, per-day, expanded; for fill-rate only
    presence_by_day = {}
    for dn in event_day_names:
        key = f'presence_{dn}'
        presence_by_day[dn] = sum(t.presence.get(key, 0) for t in cum)  # all valid rows expanded

    avg_price_ht = round(revenue_ht / tickets_sold, 2) if tickets_sold else 0.0

    return {
        'tickets_sold': tickets_sold,         # PAID, once-each  (NOT presence)
        'tickets_all': tickets_all,           # incl comps
        'tickets_free': free,
        'revenue_ht': revenue_ht,             # promoter net (recette Revenue)
        'revenue_ttc': revenue_ttc,           # buyer-facing gross
        'revenue_vat': revenue_vat,
        'avg_price_ht': avg_price_ht,
        'by_platform': {k: v for k, v in by_platform.items()},
        'by_type': {k: v for k, v in by_type.items()},
        'by_access': dict(by_access),
        'presence_by_day': presence_by_day,   # EXPANDED — per-day fill-rate ONLY
        'cutoff_cumulative': cutoff_cumulative.isoformat(),
        'cutoff_velocity': cutoff_velocity.isoformat(),
    }


def compute_velocity(tickets: list[Ticket], cutoff_velocity: date) -> dict:
    """Rolling velocity on COMPLETE days only (dual-cutoff). Paid tickets."""
    paid = [t for t in _paid(tickets) if t.order_date <= cutoff_velocity]
    vel = {}
    for window in (7, 14, 30):
        thr = cutoff_velocity - timedelta(days=window)
        vel[f'velocity_{window}d'] = round(len([t for t in paid if t.order_date > thr]) / window, 3)
    return vel


def compute_day_velocity(tickets, event_day_names, cutoff_velocity):
    """EDIT 2 — Per-day velocity (presence-based 14d rate + 7d trend vs prior 7d).
    Mirrors the standalone's day_velocity: 14d window uses presence; trend compares
    last-7d total vs the preceding 7d. Complete-day basis."""
    paid = [t for t in _paid(tickets) if t.order_date <= cutoff_velocity]
    out = {}
    d7 = cutoff_velocity - timedelta(days=7)
    d14 = cutoff_velocity - timedelta(days=14)
    for dn in event_day_names:
        key = f'presence_{dn}'
        last7 = [t for t in paid if t.order_date > d7 and t.presence.get(key, 0) == 1]
        prev7 = [t for t in paid if d14 < t.order_date <= d7 and t.presence.get(key, 0) == 1]
        last14 = [t for t in paid if t.order_date > d14 and t.presence.get(key, 0) == 1]
        vel_total_7d = round(len(last7) / 7, 3)
        vel_prev_7d = len(prev7) / 7
        trend = round((vel_total_7d - vel_prev_7d) / vel_prev_7d * 100, 1) if vel_prev_7d > 0 else 0.0
        out[dn] = {
            'velocity_14d': round(len(last14) / 14, 3),
            'velocity_total_7d': vel_total_7d,
            'trend_pct': trend,
        }
    return out


def compute_fill_rates(metrics: dict, cfg: dict) -> dict:
    """EDIT 2 — Sell-through (overall) + per-day fill %.
    Standalone definition: sell-through = total PRESENCE / total capacity * 100
    (NOT tickets_sold/capacity — presence is the attendance the cap is measured against).
    Per-day fill = day presence / day capacity * 100. Two-counts discipline preserved:
    presence is the expanded attendance, explicitly labeled."""
    day_caps = {d['day_name'].lower(): d['day_capacity'] for d in cfg['days']}
    total_cap = cfg['total_capacity']
    presence_by_day = metrics['presence_by_day']
    total_presence = sum(presence_by_day.values())

    per_day = {}
    for dn, pres in presence_by_day.items():
        cap = day_caps.get(dn, 0)
        per_day[dn] = {
            'presence': pres,                       # EXPANDED attendance (not tickets_sold)
            'capacity': cap,
            'fill_pct': round(pres / cap * 100, 1) if cap else 0.0,
            'remaining': cap - pres,
        }
    return {
        'sell_through_pct': round(total_presence / total_cap * 100, 1) if total_cap else 0.0,
        'total_presence': total_presence,           # EXPANDED (per-day fill basis ONLY)
        'total_capacity': total_cap,
        'per_day': per_day,
        '_note': 'sell_through uses PRESENCE/capacity (standalone definition); '
                 'presence != tickets_sold (see count_model)',
    }


def compute_projection(tickets: list[Ticket], cutoff_velocity: date,
                       event_date_first: date, total_capacity: int,
                       avg_price_ht: float = 0.0, total_presence: int = 0,
                       tickets_sold: int = 0) -> dict:
    """Pessimiste/base/optimiste using 7-day velocity windows + 14d base.
    Complete-day basis (cutoff_velocity). Behavior-equivalent to the standalone.
    EDIT 2: also emits revenue projection (capped tickets x avg price) +
    revenue-if-soldout, per the standalone's REVENUE_PROJECTION / REVENUE_IF_SOLDOUT."""
    paid = [t for t in _paid(tickets) if t.order_date <= cutoff_velocity]
    days_remaining = max(0, (event_date_first - cutoff_velocity).days)
    all_dates = sorted({t.order_date for t in paid})

    windows = []
    for i in range(len(all_dates) - 6):
        ws, we = all_dates[i], all_dates[i + 6]
        windows.append(len([t for t in paid if ws <= t.order_date <= we]) / 7)
    last14 = cutoff_velocity - timedelta(days=13)
    base_vel = len([t for t in paid if last14 <= t.order_date <= cutoff_velocity]) / 14
    current = len(paid)

    def scenario(vel):
        projected = int(current + vel * days_remaining)
        pct = round(projected / total_capacity * 100, 1) if total_capacity else 0.0
        return {'velocity': round(vel, 3), 'projected': projected, 'pct_capacity': pct}

    # --- revenue projection (standalone REVENUE_PROJECTION logic) ---
    # cap by capacity expressed in TICKETS: capacity / avg_presence_per_ticket.
    avg_presence_per_ticket = (total_presence / tickets_sold) if tickets_sold > 0 else 1.2
    projected_tickets = current + int(base_vel * days_remaining)
    max_tickets_for_capacity = (total_capacity / avg_presence_per_ticket
                                if avg_presence_per_ticket > 0 else total_capacity)
    capped_tickets = min(projected_tickets, max_tickets_for_capacity)
    revenue_projection = round(capped_tickets * avg_price_ht, 2)
    revenue_if_soldout = round(max_tickets_for_capacity * avg_price_ht, 2)

    return {
        'days_remaining': days_remaining,
        'current_tickets': current,
        'inputs': {'base_velocity_14d': round(base_vel, 3),
                   'best_7d_window': round(max(windows), 3) if windows else 0.0,
                   'worst_7d_window': round(min(windows), 3) if windows else 0.0,
                   'avg_presence_per_ticket': round(avg_presence_per_ticket, 4)},
        'scenarios': {
            'pessimiste': scenario(min(windows) if windows else 0.0),
            'base': scenario(base_vel),
            'optimiste': scenario(max(windows) if windows else 0.0),
        },
        'revenue_projection_ht': revenue_projection,     # EDIT 2
        'revenue_if_soldout_ht': revenue_if_soldout,     # EDIT 2
    }


# ---- Comparison-mode bucketing (MUST-SURVIVE behavior #1) -------------------

def build_suivi(tickets_cur, tickets_prev, cfg_cur, cfg_prev, cutoff_velocity):
    """EDIT 2 — Full suivi des ventes: daily rows + weekly rollup.
    Each daily row: date, sales, cumulative, Shotgun/DICE split, and the
    comparison-mode-aligned previous-year sales+cumulative. Weekly groups the
    same by ISO-ish week buckets counted back from the cutoff. Paid tickets only.
    Reuses the SAME comparison-mode matchers as the curve (no parallel logic)."""
    mode = cfg_cur.get('comparison_mode', J_MINUS)
    use_dsl = (mode == DAYS_SINCE_LAUNCH and cfg_cur.get('launch_date')
               and cfg_prev and cfg_prev.get('launch_date'))

    paid_cur = sorted(_paid(tickets_cur), key=lambda t: t.order_date)
    sales, plat = defaultdict(int), defaultdict(lambda: defaultdict(int))
    for t in paid_cur:
        sales[t.order_date] += 1
        plat[t.order_date][t.platform] += 1
    cur_dates = sorted(sales)
    cum, run = {}, 0
    for d in cur_dates:
        run += sales[d]; cum[d] = run

    # previous-year cumulative by date (for aligned lookup)
    prev_cum = {}
    if tickets_prev and cfg_prev:
        prun = 0
        pc = defaultdict(int)
        for t in _paid(tickets_prev):
            pc[t.order_date] += 1
        for d in sorted(pc):
            prun += pc[d]; prev_cum[d] = prun

    def prev_at(cur_d):
        if not prev_cum:
            return None, None
        if use_dsl:
            pd = _match_prev_dsl(cur_d, cfg_cur['event_date_last'], cfg_prev['event_date_last'])
        else:
            pd = _match_prev_jminus(cur_d, cfg_cur['event_date_first'], cfg_prev['event_date_first'])
        le = [d for d in prev_cum if d <= pd]
        cumv = prev_cum[max(le)] if le else 0
        # prev day sales = cumulative delta vs the day before the aligned date
        before = [d for d in prev_cum if d < pd]
        prev_day_cum = prev_cum[max(before)] if before else 0
        return cumv - prev_day_cum, cumv

    daily = []
    for d in cur_dates:
        psales, pcum = prev_at(d)
        daily.append({
            'date': d.isoformat(),
            'sales': sales[d],
            'cumulative': cum[d],
            'shotgun': plat[d].get('Shotgun', 0),
            'dice': plat[d].get('DICE', 0),
            'prev_sales': psales,
            'prev_cumulative': pcum,
        })

    # weekly rollup: bucket days into weeks counting back from the last date
    weekly = []
    if cur_dates:
        last = cur_dates[-1]
        wk = defaultdict(lambda: {'sales': 0, 'prev_sales': 0, 'start': None, 'end': None})
        for row in daily:
            d = date.fromisoformat(row['date'])
            wnum = (last - d).days // 7          # 0 = most recent week
            b = wk[wnum]
            b['sales'] += row['sales']
            b['prev_sales'] += (row['prev_sales'] or 0)
            b['start'] = min(b['start'], d) if b['start'] else d
            b['end'] = max(b['end'], d) if b['end'] else d
        for wnum in sorted(wk):
            b = wk[wnum]
            weekly.append({
                'weeks_before_cutoff': wnum,
                'start': b['start'].isoformat(), 'end': b['end'].isoformat(),
                'sales': b['sales'], 'prev_sales': b['prev_sales'],
            })
    return {'mode': mode, 'daily': daily, 'weekly': weekly}


def compute_yoy_deltas(metrics_cur, fill_cur, metrics_prev, fill_prev):
    """EDIT 2 — Year-over-year delta %s per metric (tickets, revenue, presence,
    avg price). Pure arithmetic on already-computed totals. None when no reference."""
    if not metrics_prev:
        return None
    def pct(cur, prev):
        return round((cur - prev) / prev * 100, 1) if prev else None
    return {
        'tickets_sold_pct': pct(metrics_cur['tickets_sold'], metrics_prev['tickets_sold']),
        'tickets_sold_diff': metrics_cur['tickets_sold'] - metrics_prev['tickets_sold'],
        'revenue_ht_pct': pct(metrics_cur['revenue_ht'], metrics_prev['revenue_ht']),
        'revenue_ht_diff': round(metrics_cur['revenue_ht'] - metrics_prev['revenue_ht'], 2),
        'presence_pct': pct(fill_cur['total_presence'], fill_prev['total_presence']) if fill_prev else None,
        'avg_price_ht_pct': pct(metrics_cur['avg_price_ht'], metrics_prev['avg_price_ht']),
        'avg_price_ht_diff': round(metrics_cur['avg_price_ht'] - metrics_prev['avg_price_ht'], 2),
    }


def derive_launch_date(tickets: list[Ticket]) -> Optional[date]:
    """launch_date = min(order_date). Derived, not config. Needs per-ticket dates."""
    ds = [t.order_date for t in tickets]
    return min(ds) if ds else None


def _match_prev_jminus(current_date, event_first_cur, event_first_prev):
    """j_minus: same days-before-event, then shift to same weekday."""
    j_x = (event_first_cur - current_date).days
    cand = event_first_prev - timedelta(days=j_x)
    wd = current_date.weekday() - cand.weekday()
    if wd > 3: wd -= 7
    if wd < -3: wd += 7
    return cand + timedelta(days=wd)


def _match_prev_dsl(current_date, event_last_cur, event_last_prev):
    """days_since_launch: anchor by distance from event-end+1, weekday-matched."""
    cur_end = event_last_cur + timedelta(days=1)
    prev_end = event_last_prev + timedelta(days=1)
    days_before_end = (cur_end - current_date).days
    cand = prev_end - timedelta(days=days_before_end)
    wd = current_date.weekday() - cand.weekday()
    if wd > 3: wd -= 7
    if wd < -3: wd += 7
    return cand + timedelta(days=wd)


def build_timeseries(tickets_cur, tickets_prev, cfg_cur, cfg_prev, cutoff_velocity):
    """Daily cumulative series for current + comparison event, bucketed by the
    event's comparison_mode. Returns dated rows the curve consumes."""
    mode = cfg_cur.get('comparison_mode', J_MINUS)
    paid_cur = sorted(_paid(tickets_cur), key=lambda t: t.order_date)

    # current cumulative by date
    cur_daily, run = {}, 0
    cur_count = defaultdict(int)
    for t in paid_cur:
        cur_count[t.order_date] += 1
    for d in sorted(cur_count):
        run += cur_count[d]
        cur_daily[d] = run

    series = []
    rows_dates = sorted(cur_count)
    if not rows_dates:
        return {'mode': mode, 'rows': []}

    # previous cumulative by date (for mapping)
    prev_daily = {}
    if tickets_prev and cfg_prev:
        prun = 0
        pc = defaultdict(int)
        for t in _paid(tickets_prev):
            pc[t.order_date] += 1
        for d in sorted(pc):
            prun += pc[d]
            prev_daily[d] = prun

    use_dsl = (mode == DAYS_SINCE_LAUNCH and cfg_cur.get('launch_date') and cfg_prev and cfg_prev.get('launch_date'))

    def prev_cumulative_at(cur_d):
        if not prev_daily:
            return None
        if use_dsl:
            pd = _match_prev_dsl(cur_d, cfg_cur['event_date_last'], cfg_prev['event_date_last'])
        else:
            pd = _match_prev_jminus(cur_d, cfg_cur['event_date_first'], cfg_prev['event_date_first'])
        # cumulative as-of pd: largest prev date <= pd
        candidates = [d for d in prev_daily if d <= pd]
        return prev_daily[max(candidates)] if candidates else 0

    for d in rows_dates:
        series.append({
            'date': d.isoformat(),
            'cumulative_current': cur_daily[d],
            'cumulative_reference': prev_cumulative_at(d),
        })
    return {'mode': mode, 'rows': series}


# ============================================================================
# JSON CONTRACT EMITTER
# ============================================================================

def emit_contract(tickets_cur, cfg_cur, tickets_prev=None, cfg_prev=None) -> dict:
    """Assemble the full JSON the drawer + recette consume."""
    day_names = [d['day_name'].lower() for d in cfg_cur['days']]
    apply_presence(tickets_cur, day_names)
    if tickets_prev:
        apply_presence(tickets_prev, [d['day_name'].lower() for d in cfg_prev['days']])

    cutoff_cum, cutoff_vel = dual_cutoff(tickets_cur)
    cfg_cur = dict(cfg_cur)
    cfg_cur['launch_date'] = derive_launch_date(tickets_cur)
    if cfg_prev:
        cfg_prev = dict(cfg_prev)
        cfg_prev['launch_date'] = derive_launch_date(tickets_prev)

    metrics = compute_metrics(tickets_cur, day_names, cutoff_cum, cutoff_vel)
    fill = compute_fill_rates(metrics, cfg_cur)                         # EDIT 2
    velocity = compute_velocity(tickets_cur, cutoff_vel)
    day_velocity = compute_day_velocity(tickets_cur, day_names, cutoff_vel)  # EDIT 2
    projection = compute_projection(                                   # EDIT 2 (revenue proj)
        tickets_cur, cutoff_vel, cfg_cur['event_date_first'], cfg_cur['total_capacity'],
        avg_price_ht=metrics['avg_price_ht'],
        total_presence=fill['total_presence'], tickets_sold=metrics['tickets_sold'])
    timeseries = build_timeseries(tickets_cur, tickets_prev, cfg_cur, cfg_prev, cutoff_vel)
    suivi = build_suivi(tickets_cur, tickets_prev, cfg_cur, cfg_prev, cutoff_vel)  # EDIT 2

    last_dt = max((t.order_datetime for t in tickets_cur if t.order_datetime), default=None)

    comparison = None
    yoy = None
    if tickets_prev and cfg_prev:
        prev_day_names = [d['day_name'].lower() for d in cfg_prev['days']]
        m_prev = compute_metrics(tickets_prev, prev_day_names, *dual_cutoff(tickets_prev))
        fill_prev = compute_fill_rates(m_prev, cfg_prev)
        yoy = compute_yoy_deltas(metrics, fill, m_prev, fill_prev)      # EDIT 2
        comparison = {
            'reference_event_id': cfg_prev.get('event_id'),
            'mode': cfg_cur.get('comparison_mode', J_MINUS),
            'reference_tickets_sold': m_prev['tickets_sold'],
            'reference_revenue_ht': m_prev['revenue_ht'],
            'reference_presence_total': fill_prev['total_presence'],
            'yoy_deltas': yoy,                                          # EDIT 2
        }

    return {
        'event_id': cfg_cur.get('event_id'),
        'event_name': cfg_cur.get('event_name'),
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'last_ticket_sold': last_dt.isoformat() if last_dt else None,
        'cutoffs': {'cumulative': cutoff_cum.isoformat(), 'velocity': cutoff_vel.isoformat()},
        'money_model': {
            'dice': 'gross=TTC(face); HT=TTC/1.055; VAT=TTC-HT; commission buyer-paid (not in gross)',
            'shotgun': 'deal_price=HT(promoter net); service/user fees buyer-paid (TTC) not in net',
            'vat_rate': VAT_RATE,
        },
        'count_model': {
            'tickets_sold': 'valid/scanned & paid, counted ONCE (headline + recette + settlement)',
            'presence': 'each ticket expanded across granted days — per-day fill-rate ONLY',
        },
        'capacity_source': {                                # EDIT 3 — FLAG for CC
            'source': 'event_config.csv (hand-entered)',
            'reconcile_on_live': 'On first live pull, compare DICE PriceTier.allocation / '
                                 'TicketPool.allocation / Event.totalTicketAllocationQty against '
                                 'config capacities. If they match, adopt API allocation; if not, '
                                 'it is a finding (API allocation may be gross inventory vs the '
                                 'marketing capacity the fill-rate cards intend) — keep config.',
            'config_total_capacity': cfg_cur['total_capacity'],
        },
        'totals': metrics,
        'fill_rates': fill,                                 # EDIT 2: sell-through + per-day fill
        'velocity': velocity,
        'day_velocity': day_velocity,                       # EDIT 2: per-day velocity + trend
        'projection': projection,                           # EDIT 2: + revenue projection
        'timeseries': timeseries,
        'suivi': suivi,                                     # EDIT 2: daily + weekly table
        'comparison': comparison,                           # EDIT 2: + yoy_deltas
    }


# ============================================================================
# CLI  (offline run against CSV fixtures; live run wires API adapters in CC)
# ============================================================================

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description="Billetterie data layer — emit JSON contract")
    ap.add_argument('--shotgun-csv', help='current-event Shotgun valid_orders CSV')
    ap.add_argument('--dice-csv', help='current-event DICE doorlist/merged CSV')
    ap.add_argument('--ref-shotgun-csv', help='reference-event Shotgun CSV')
    ap.add_argument('--ref-dice-csv', help='reference-event DICE CSV')
    ap.add_argument('--out', default='contract.json')
    args = ap.parse_args()

    # Minimal config example (bordeaux_2026). In the repo this comes from event_config.csv.
    cfg = {
        'event_id': 'bordeaux_2026', 'event_name': 'Sonora Bordeaux 2026',
        'comparison_mode': J_MINUS, 'compare_to': 'bordeaux_2025',
        'days': [
            {'day_number': 1, 'day_name': 'Jeudi', 'day_date': date(2026, 6, 11), 'day_capacity': 8500},
            {'day_number': 2, 'day_name': 'Vendredi', 'day_date': date(2026, 6, 12), 'day_capacity': 18000},
            {'day_number': 3, 'day_name': 'Samedi', 'day_date': date(2026, 6, 13), 'day_capacity': 18000},
        ],
        'event_date_first': date(2026, 6, 11), 'event_date_last': date(2026, 6, 13),
        'total_capacity': 44500,
    }

    cur = []
    if args.shotgun_csv:
        cur += ShotgunCSVAdapter(args.shotgun_csv, cfg['days']).fetch()
    if args.dice_csv:
        cur += DiceCSVAdapter(args.dice_csv, cfg['days']).fetch()

    contract = emit_contract(cur, cfg)
    with open(args.out, 'w') as f:
        json.dump(contract, f, indent=2, default=str)
    print(f"Wrote {args.out}: tickets_sold={contract['totals']['tickets_sold']} "
          f"revenue_ht={contract['totals']['revenue_ht']}")
