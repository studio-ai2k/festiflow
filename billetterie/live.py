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

# Live Shotgun ticket_status allow-list. Per /tickets doc p4 the enum is
# valid, resold, refunded, canceled, payment_plan_pending, pending_app… —
# there is NO 'scanned' status (scanning is the ticket_scanned_at timestamp).
# Keep only 'valid' AND require ticket_canceled_at is null: this excludes
# refunded/pending/canceled and avoids double-counting 'resold' (seller's
# original + buyer's new row → the live admission is the 'valid' row, counted
# once). Offline CSV is all-'valid', so validate.py 17/17 is unaffected.
SHOTGUN_KEEP_STATUS = ('valid',)


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

# Event-node query — capacity/allocation reconciliation only (DICE inventory).
_DICE_ALLOC_GQL = """
query Alloc($eventId: ID!) {
  node(id: $eventId) { ... on Event {
    name totalTicketAllocationQty
    ticketPools { name allocation }
    tickets(first: 1) { totalCount }
  } }
}
"""

# Event-scoped orders — the production dating path (DICE doc p33/p35):
#   viewer.orders(where: {eventId: {eq: ...}})  -> no full-promoter scan
#   Order.purchasedAt = true sale time;  Order.tickets is a LIST;
#   Order.returns[].ticketId flags returned tickets to exclude;
#   Order.salesChannel = INTERNET|DOOR|FREE (structured comp flag).
_DICE_ORDERS_GQL = """
query Orders($where: OrderWhereInput, $first: Int, $after: String) {
  viewer { orders(where: $where, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      purchasedAt salesChannel
      returns { ticketId }
      tickets {
        id fullPrice total
        ticketType { name }
        fees { category dice promoter }
      }
    } }
  } }
}
"""


class LiveDiceAdapter:
    """Live DICE via event-scoped viewer.orders(where:{eventId:{eq:...}}).
    Dates each ticket by Order.purchasedAt (true sale time), excludes returned
    tickets (Order.returns[].ticketId), and reuses dl.normalize_dice_type +
    dl.dice_money (engine untouched). Also fetches Event allocation for capacity
    reconciliation, and tracks token-scope health (null purchasedAt/face)."""

    def __init__(self, client, dice_event_id, event_days, page_size=100, where_id_mode='relay'):
        # where_id_mode default 'relay': recon PROVED OrderWhereInput.eventId.eq
        # wants the base64 relay global id — numeric was rejected ("Could not
        # decode ID value '540197'"). eventId type is OperatorsIdInput.
        self.client = client
        self.raw_id = str(dice_event_id).strip()
        self.relay = dice_relay_id(dice_event_id)
        self.event_days = event_days
        self.page_size = page_size
        self.where_id_mode = where_id_mode  # 'numeric' | 'relay'
        # capacity / observability (populated during fetch):
        self.total_allocation = None
        self.ticket_pools = []
        self.declared_total_count = None
        self.fee_categories = set()
        self.sales_channels = set()
        self.orders_seen = 0
        self.returned_excluded = 0
        self.null_purchased_at = 0
        self.null_face = 0

    def _where_id(self):
        return self.relay if self.where_id_mode == 'relay' else self.raw_id

    def _load_allocation(self):
        try:
            data = self.client.query(_DICE_ALLOC_GQL, {'eventId': self.relay})
            node = data.get('node') or {}
            self.total_allocation = node.get('totalTicketAllocationQty')
            self.ticket_pools = node.get('ticketPools') or []
            self.declared_total_count = (node.get('tickets') or {}).get('totalCount')
        except Exception:
            pass

    def fetch(self):
        self._load_allocation()
        day_dates = [d['day_date'] for d in self.event_days if d.get('day_date')]
        fallback = min(day_dates) if day_dates else date.today()
        out = []
        cursor = None
        where = {'eventId': {'eq': self._where_id()}}
        while True:
            data = self.client.query(_DICE_ORDERS_GQL,
                                     {'where': where, 'first': self.page_size, 'after': cursor})
            conn = ((data.get('viewer') or {}).get('orders') or {})
            for e in (conn.get('edges') or []):
                order = e.get('node') or {}
                self.orders_seen += 1
                purchased = _parse_dt(order.get('purchasedAt'))
                if order.get('purchasedAt') is None:
                    self.null_purchased_at += 1
                channel = order.get('salesChannel') or ''
                if channel:
                    self.sales_channels.add(channel)
                returned = {r.get('ticketId') for r in (order.get('returns') or [])}
                for n in (order.get('tickets') or []):
                    if n.get('id') in returned:
                        self.returned_excluded += 1
                        continue
                    face_cents = n.get('fullPrice')
                    if face_cents is None:
                        face_cents = n.get('total')
                    if face_cents is None:
                        self.null_face += 1
                        face_cents = 0
                    face = face_cents / 100.0
                    for fee in (n.get('fees') or []):
                        if fee.get('category'):
                            self.fee_categories.add(fee['category'])
                    structured = (n.get('ticketType') or {}).get('name') or ''
                    tt, access, att_days, product_name, _src = dl.normalize_dice_type(
                        structured, face, self.event_days)
                    # salesChannel FREE is the clean structured comp flag (doc p33)
                    is_paid = 0 if (channel == 'FREE' or access in ('invitation', 'jeu_concours')
                                    or face == 0) else 1
                    gross_ttc, net_ht, vat = dl.dice_money(face)
                    out.append(dl.Ticket(
                        order_date=(purchased.date() if purchased else fallback),
                        order_datetime=purchased, platform='DICE',
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
    numeric = str(dice_event_id).strip()
    print(f"  event {dice_event_id} -> relay {relay}")

    # 1) introspection: OrderWhereInput (eventId filter), Order.purchasedAt, VAT enum
    introspection = """
    query {
      owi: __type(name:"OrderWhereInput"){ inputFields { name type { kind name ofType { kind name } } } }
      vat: __type(name:"TicketFeeCategory"){ enumValues(includeDeprecated:true){ name } }
    }"""
    try:
        env = client.raw(introspection)
        data = env.get('data') or {}
        owi = data.get('owi') or {}
        names = [f['name'] for f in (owi.get('inputFields') or [])]
        print("  OrderWhereInput fields:", names)
        evf = next((f for f in (owi.get('inputFields') or []) if f['name'] == 'eventId'), None)
        if evf:
            ty = evf['type']
            print("  OrderWhereInput.eventId type:", ty.get('kind'), ty.get('name'),
                  "ofType:", (ty.get('ofType') or {}).get('name'))
        vat = data.get('vat') or {}
        print("  TicketFeeCategory has SALES_TAX:",
              'SALES_TAX' in [e['name'] for e in (vat.get('enumValues') or [])])
        if env.get('errors'):
            print("  [introspection errors]", env['errors'][:2])
    except Exception as e:
        print("  introspection failed:", e)

    # 2) probe event-scoped orders with relay vs numeric eventId; verify token scope
    probe = """
    query($where: OrderWhereInput, $first: Int){
      viewer { orders(where:$where, first:$first){
        pageInfo { hasNextPage }
        edges { node { purchasedAt salesChannel total
          returns { ticketId }
          tickets { id fullPrice total ticketType { name } fees { category } } } }
      } }
    }"""
    # numeric first (high-probability per the where-filter type system), relay fallback
    for mode, idval in (('numeric', numeric), ('relay', relay)):
        try:
            env = client.raw(probe, {"where": {"eventId": {"eq": idval}}, "first": 3})
            if env.get('errors'):
                print(f"  [where eventId={mode}] REJECTED -> {str(env['errors'][0].get('message'))[:140]}")
                continue
            conn = (((env.get('data') or {}).get('viewer') or {}).get('orders') or {})
            edges = conn.get('edges') or []
            print(f"  [where eventId={mode}] OK: {len(edges)} order(s) on first page; "
                  f"hasNextPage={(conn.get('pageInfo') or {}).get('hasNextPage')}")
            if edges:
                o = edges[0].get('node') or {}
                tks = o.get('tickets') or []
                t0 = tks[0] if tks else {}
                print(f"    TOKEN-SCOPE check: purchasedAt={o.get('purchasedAt')!r} "
                      f"salesChannel={o.get('salesChannel')!r} order.total={o.get('total')!r} "
                      f"ticket.fullPrice={t0.get('fullPrice')!r} ticket.total={t0.get('total')!r}")
                tt_name = (t0.get('ticketType') or {}).get('name')
                print(f"    sample type={tt_name!r} fees={t0.get('fees')}")
        except Exception as e:
            print(f"  [where eventId={mode}] probe error: {e}")

    # 3) event-node allocation (capacity reconciliation input)
    alloc = """query($eventId: ID!){ node(id:$eventId){ ... on Event {
      totalTicketAllocationQty ticketPools { name allocation } tickets { totalCount } } } }"""
    try:
        data = client.query(alloc, {"eventId": relay})
        node = data.get('node') or {}
        print(f"  Event.totalTicketAllocationQty = {node.get('totalTicketAllocationQty')}")
        print(f"  Event.tickets.totalCount = {(node.get('tickets') or {}).get('totalCount')}")
    except Exception as e:
        print("  allocation probe failed:", e)
