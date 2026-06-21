#!/usr/bin/env python3
"""
live.py — LIVE API wiring for the billetterie data layer.
=========================================================
The validated engine lives in billetterie_datalayer.py and is NOT touched here
(its money math, classification, presence, comparison bucketing, emit_contract
are the floor). This module only supplies:

  - stdlib HTTP clients (Shotgun REST, DICE GraphQL) — no third-party deps.
  - LIVE adapters that produce the IDENTICAL `dl.Ticket` record the CSV adapters
    produce, by REUSING the engine helpers (dl.classify_ticket, dl.shotgun_money,
    dl.normalize_dice_type, dl.dice_money).
  - recon helpers that probe the real schema before we trust a number.

Why not use dl.ShotgunAPIAdapter / dl.DiceAPIAdapter directly?
  Their I/O shapes were written against an assumed schema that the LIVE schema
  contradicts (verified by introspection + yesterday's proven interim fetch):
    * Shotgun live envelope returns the ticket array under `data`, not `tickets`.
    * DICE `viewer.orders` has NO `eventId` arg and `Order.tickets` is a LIST,
      not a connection — so `viewer.orders(eventId:){ tickets{ edges } }` is
      invalid AND would require scanning every promoter order (the 15-min hang).
  The proven path (yesterday's interim fetch) is the event-node tickets
  connection with Bearer auth. We wire to THAT, reusing the validated record
  builders so the engine + contract stay source-agnostic.

DICE dating: the event-node Ticket exposes `claimedAt`, not `Order.purchasedAt`.
  Per the integration decision, DICE tickets are dated by `claimedAt` (fast,
  event-scoped, ~1-2 min). This is dating-only: the settlement gate
  (count / gross / HT / VAT) and YoY totals are unaffected; only the DICE
  purchase-over-time curve granularity is approximate. Flagged in the contract.
"""

from __future__ import annotations

import base64
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

import billetterie_datalayer as dl


SHOTGUN_TICKETS_URL = "https://api.shotgun.live/tickets"
DICE_GRAPHQL_ENDPOINT = "https://partners-endpoint.dice.fm/graphql"

# Live Shotgun ticket_status vocabulary kept (non-cancelled, real tickets).
# Superset of the CSV export vocab; recon dumps the live distribution so this
# can be tightened if the paid count overshoots the settlement floor.
SHOTGUN_KEEP_STATUS = ('valid', 'scanned', 'resold')


# ============================================================================
# HTTP CLIENTS (stdlib only)
# ============================================================================

class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return json.loads(self._body)


def _http(url, *, data=None, headers=None, method='GET', timeout=60, retries=3):
    headers = headers or {}
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 and attempt < retries:
                time.sleep(5 * attempt); continue
            if e.code >= 500 and attempt < retries:
                time.sleep(3 * attempt); continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < retries:
                time.sleep(3 * attempt); continue
            raise
    raise last  # pragma: no cover


class ShotgunHTTPClient:
    """requests-like .get(url, params) -> obj with .json(). Adds token, paces."""

    def __init__(self, token, pace=0.8, timeout=30, retries=3):
        self.token = token
        self.pace = pace
        self.timeout = timeout
        self.retries = retries
        self._calls = 0

    def get(self, url, params=None):
        if params is not None:
            p = dict(params)
            p.setdefault('token', self.token)
            url = url + ('&' if '?' in url else '?') + urllib.parse.urlencode(p)
        elif 'token=' not in url:
            url = url + ('&' if '?' in url else '?') + urllib.parse.urlencode({'token': self.token})
        if self._calls > 0:
            time.sleep(self.pace)
        self._calls += 1
        return _Resp(_http(url, timeout=self.timeout, retries=self.retries))


class DiceGraphQLClient:
    def __init__(self, token, timeout=60, retries=3):
        self.token = token
        self.timeout = timeout
        self.retries = retries

    def _post(self, payload):
        body = json.dumps(payload).encode('utf-8')
        raw = _http(DICE_GRAPHQL_ENDPOINT, data=body, method='POST',
                    headers={'Authorization': f'Bearer {self.token}',
                             'Content-Type': 'application/json'},
                    timeout=self.timeout, retries=self.retries)
        return json.loads(raw)

    def query(self, gql, variables=None):
        resp = self._post({'query': gql, 'variables': variables or {}})
        if resp.get('errors'):
            raise RuntimeError(f"DICE GraphQL errors: {resp['errors']}")
        return resp.get('data') or {}

    def raw(self, gql, variables=None):
        """Like query() but returns the full envelope (errors included) — for recon."""
        return self._post({'query': gql, 'variables': variables or {}})


# ============================================================================
# DICE relay-id helper
# ============================================================================

def dice_relay_id(numeric_or_relay):
    rid = str(numeric_or_relay).strip()
    return rid if rid.startswith('RXZlbnQ6') else base64.b64encode(f'Event:{rid}'.encode()).decode()


def _parse_dt(s):
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


# ============================================================================
# LIVE SHOTGUN ADAPTER  (envelope='data', token auth, paced)
# ============================================================================

class LiveShotgunAdapter:
    """Live Shotgun REST -> List[dl.Ticket]. Reuses dl.classify_ticket /
    dl.shotgun_money so records are identical to the validated CSV adapter."""

    def __init__(self, client, organizer_id, event_id, event_days,
                 keep_status=SHOTGUN_KEEP_STATUS):
        self.client = client
        self.organizer_id = organizer_id
        self.event_id = event_id
        self.event_days = event_days
        self.keep_status = set(keep_status)

    def _iter(self):
        url = SHOTGUN_TICKETS_URL
        params = {"organizer_id": self.organizer_id, "event_id": self.event_id,
                  "include_cohosted_events": "false"}
        while url:
            resp = self.client.get(url, params=params).json()
            # Live envelope: array under `data` (proven by interim fetch); accept
            # `tickets` as a fallback in case the API ever changes.
            rows = resp.get('data')
            if rows is None:
                rows = resp.get('tickets', [])
            for t in rows:
                yield t
            nxt = (resp.get('pagination') or {}).get('next')
            url, params = (nxt, None) if nxt else (None, None)

    def fetch(self):
        out = []
        for t in self._iter():
            if (t.get('ticket_status') or '') not in self.keep_status:
                continue
            if t.get('ticket_canceled_at'):
                continue
            odt = _parse_dt(t.get('ordered_at'))
            if not odt:
                continue
            category = t.get('deal_sub_category', '') or ''
            deal_title = t.get('deal_title', '') or ''
            deal_channel = t.get('deal_channel', '') or ''
            combined = f"{category} {deal_title}".strip()

            ht = (t.get('deal_price') or 0) / 100.0
            fees = ((t.get('deal_service_fee') or 0) + (t.get('deal_user_service_fee') or 0)) / 100.0
            ttc = round(ht + fees, 2)

            tt, access, att_days, _ = dl.classify_ticket(
                combined, price=ttc, tags='invitation' if deal_channel == 'invitation' else '',
                event_days=self.event_days)
            product_name = category.title() if category.isupper() else category

            # day fallback from event start time (mirror CSV adapter behavior)
            if (not att_days) and tt == 'single_day':
                d = dl.determine_day_from_dates(t.get('event_start_time', ''))
                if d:
                    tt = d; att_days = [d]

            is_paid = 1 if ht > 0 else 0
            _, net_ht, vat = dl.shotgun_money(ht)
            out.append(dl.Ticket(
                order_date=odt.date(), order_datetime=odt, platform='Shotgun',
                ticket_type=tt, access_level=access, attendance_days=att_days,
                product_name=product_name,
                gross_ttc=ttc, net_ht=net_ht, vat=vat, is_paid=is_paid,
            ))
        return out


# ============================================================================
# LIVE DICE ADAPTER  (event-node tickets connection, claimedAt dating)
# ============================================================================

_EVENT_TICKETS_GQL = """
query Ev($eventId: ID!, $first: Int!, $after: String) {
  node(id: $eventId) {
    ... on Event {
      name
      totalTicketAllocationQty
      ticketPools { name allocation }
      tickets(first: $first, after: $after) {
        totalCount
        pageInfo { hasNextPage endCursor }
        edges { node {
          id code fullPrice total claimedAt
          ticketType { name }
          fees { category dice promoter }
        } }
      }
    }
  }
}
"""


class LiveDiceAdapter:
    """Live DICE GraphQL -> List[dl.Ticket] via the event-node tickets connection
    (proven path). Reuses dl.normalize_dice_type + dl.dice_money. Dates by
    claimedAt (event-scoped; Order.purchasedAt is not reachable per-event without
    the full-promoter order scan). Captures allocation for capacity reconciliation
    and the live fee categories for VAT-enum completeness."""

    def __init__(self, client, dice_event_id, event_days, page_size=100):
        self.client = client
        self.relay = dice_relay_id(dice_event_id)
        self.raw_id = str(dice_event_id).strip()
        self.event_days = event_days
        self.page_size = page_size
        # populated during fetch() for the recon/capacity reports:
        self.total_allocation = None
        self.ticket_pools = []
        self.declared_total_count = None
        self.fee_categories = set()
        self.undated = 0

    def fetch(self):
        out = []
        cursor = None
        day_dates = [d['day_date'] for d in self.event_days if d.get('day_date')]
        fallback = (min(day_dates).isoformat() if day_dates else date.today().isoformat())
        first_page = True
        while True:
            data = self.client.query(_EVENT_TICKETS_GQL,
                                     {'eventId': self.relay, 'first': self.page_size, 'after': cursor})
            node = data.get('node') or {}
            if first_page:
                self.total_allocation = node.get('totalTicketAllocationQty')
                self.ticket_pools = node.get('ticketPools') or []
                first_page = False
            conn = node.get('tickets') or {}
            if self.declared_total_count is None:
                self.declared_total_count = conn.get('totalCount')
            for e in (conn.get('edges') or []):
                n = e.get('node') or {}
                face_cents = n.get('fullPrice')
                if face_cents is None:
                    face_cents = n.get('total') or 0
                face = (face_cents or 0) / 100.0
                for fee in (n.get('fees') or []):
                    if fee.get('category'):
                        self.fee_categories.add(fee['category'])
                structured = (n.get('ticketType') or {}).get('name') or ''
                tt, access, att_days, product_name, _src = dl.normalize_dice_type(
                    structured, face, self.event_days)
                claimed = n.get('claimedAt') or ''
                od = claimed[:10] if claimed else fallback
                if not claimed:
                    self.undated += 1
                odt = _parse_dt(claimed)
                is_paid = 0 if access in ('invitation', 'jeu_concours') or face == 0 else 1
                gross_ttc, net_ht, vat = dl.dice_money(face)
                out.append(dl.Ticket(
                    order_date=date.fromisoformat(od), order_datetime=odt, platform='DICE',
                    ticket_type=tt, access_level=access, attendance_days=att_days,
                    product_name=product_name,
                    gross_ttc=gross_ttc, net_ht=net_ht, vat=vat, is_paid=is_paid,
                ))
            pi = conn.get('pageInfo') or {}
            if pi.get('hasNextPage'):
                cursor = pi.get('endCursor')
            else:
                break
        return out


# ============================================================================
# RECON  (prove the schema + each query path before trusting numbers)
# ============================================================================

def recon_shotgun(client, organizer_id, event_id, max_pages=3):
    print("\n--- SHOTGUN live recon ---")
    url = SHOTGUN_TICKETS_URL
    params = {"organizer_id": organizer_id, "event_id": event_id, "include_cohosted_events": "false"}
    seen_status = {}
    fields = None
    total = 0
    for page in range(max_pages):
        resp = client.get(url, params=params).json()
        if page == 0:
            print("  envelope keys:", list(resp.keys()))
            print("  pagination present:", bool((resp.get('pagination') or {}).get('next')))
        rows = resp.get('data')
        if rows is None:
            rows = resp.get('tickets', [])
        if rows and fields is None:
            fields = sorted(rows[0].keys())
            print(f"  ticket field count: {len(fields)}")
            money = {k: rows[0].get(k) for k in
                     ('deal_price', 'deal_service_fee', 'deal_user_service_fee',
                      'deal_vat_rate', 'deal_sub_category', 'deal_title', 'ordered_at',
                      'ticket_status', 'ticket_canceled_at') if k in rows[0]}
            print("  sample money/meta fields:", money)
        for t in rows:
            seen_status[t.get('ticket_status')] = seen_status.get(t.get('ticket_status'), 0) + 1
            total += 1
        nxt = (resp.get('pagination') or {}).get('next')
        if not nxt:
            break
        url, params = nxt, None
    print(f"  scanned {total} tickets over <= {max_pages} pages")
    print("  ticket_status distribution (sample):", seen_status)


def recon_dice(client, dice_event_id):
    print("\n--- DICE live recon ---")
    relay = dice_relay_id(dice_event_id)
    print(f"  event {dice_event_id} -> relay {relay}")

    # 1) introspection: Viewer.orders args, Order.tickets type kind, VAT enum
    introspection = """
    query {
      viewer: __type(name:"Viewer"){ fields { name args { name } } }
      order:  __type(name:"Order"){ fields { name type { kind name ofType { kind name } } } }
      vat:    __type(name:"TicketFeeCategory"){ kind enumValues(includeDeprecated:true){ name } }
    }"""
    try:
        env = client.raw(introspection)
        data = env.get('data') or {}
        v = data.get('viewer') or {}
        orders_field = next((f for f in (v.get('fields') or []) if f['name'] == 'orders'), None)
        print("  Viewer.orders args:", [a['name'] for a in (orders_field.get('args') or [])]
              if orders_field else "<no orders field>")
        o = data.get('order') or {}
        tickets_field = next((f for f in (o.get('fields') or []) if f['name'] == 'tickets'), None)
        if tickets_field:
            ty = tickets_field['type']
            print("  Order.tickets type:", ty.get('kind'), ty.get('name'),
                  "ofType:", (ty.get('ofType') or {}).get('kind'), (ty.get('ofType') or {}).get('name'))
        vat = data.get('vat') or {}
        print("  TicketFeeCategory enumValues:", [e['name'] for e in (vat.get('enumValues') or [])])
        if env.get('errors'):
            print("  [introspection errors]", env['errors'])
    except Exception as e:
        print("  introspection failed:", e)

    # 2) prove the NEW adapter's path (viewer.orders(eventId:)) FAILS
    bad = """query($eventId: ID!){ viewer { orders(eventId:$eventId){ edges { node { purchasedAt } } } } }"""
    env = client.raw(bad, {"eventId": relay})
    if env.get('errors'):
        print("  [PROVEN] viewer.orders(eventId:) REJECTED ->",
              env['errors'][0].get('message', env['errors'][0])[:160])
    else:
        print("  [UNEXPECTED] viewer.orders(eventId:) accepted; keys:",
              list(((env.get('data') or {}).get('viewer') or {}).keys()))

    # 3) prove the event-node tickets path WORKS (totalCount + allocation + sample)
    probe = """
    query($eventId: ID!){ node(id:$eventId){ ... on Event {
      name totalTicketAllocationQty
      ticketPools { name allocation }
      tickets(first: 3){ totalCount edges { node {
        fullPrice total claimedAt ticketType { name } fees { category dice promoter } } } }
    } } }"""
    try:
        data = client.query(probe, {"eventId": relay})
        node = data.get('node') or {}
        conn = node.get('tickets') or {}
        print(f"  [PROVEN] event-node tickets totalCount = {conn.get('totalCount')}")
        print(f"  Event.totalTicketAllocationQty = {node.get('totalTicketAllocationQty')}")
        print(f"  Event.ticketPools = {node.get('ticketPools')}")
        edges = conn.get('edges') or []
        if edges:
            n = edges[0].get('node') or {}
            print("  sample ticket: fullPrice(cents)=%s total(cents)=%s type=%r fees=%s claimedAt=%s" % (
                n.get('fullPrice'), n.get('total'),
                (n.get('ticketType') or {}).get('name'), n.get('fees'), n.get('claimedAt')))
    except Exception as e:
        print("  event-node probe failed:", e)
