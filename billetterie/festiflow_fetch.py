#!/usr/bin/env python3
"""
festiflow_fetch.py — Live API fetch + structured JSON output for Festiflow Billetterie.

Replaces the CSV-upload pipeline with direct API pulls from Shotgun and DICE,
runs the same classification logic as run.py, and outputs a structured JSON
per the agreed schema.

Usage:
    SHOTGUN_TOKEN=xxx DICE_TOKEN=xxx python festiflow_fetch.py epk_2026
    SHOTGUN_TOKEN=xxx DICE_TOKEN=xxx python festiflow_fetch.py --all-active

Environment variables:
    SHOTGUN_TOKEN         - Shotgun API token (from Smartboard > Integrations)
    SHOTGUN_ORGANIZER_ID  - Shotgun organizer ID (default: 171835)
    DICE_TOKEN            - DICE MIO API token (promoter-level)

Output:
    Writes JSON to stdout (pipe to file) or to --output path.
    JSON follows the agreed per-event schema with gross/fee/VAT separation
    at every breakdown level.

Authors: Leo & Claude
Date: June 2026
"""

import json
import csv
import os
import sys
import re
import time
import ssl
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, date, timedelta
from collections import defaultdict
from pathlib import Path


# ============================================================================
# CONSTANTS
# ============================================================================

ALL_DAYS = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche']

SHOTGUN_API_BASE = 'https://api.shotgun.live'
DICE_GRAPHQL_ENDPOINT = 'https://partners-endpoint.dice.fm/graphql'

DEFAULT_ORGANIZER_ID = '171835'

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5
REQUEST_TIMEOUT = 30

VERSION = '1.0.0'


# ============================================================================
# UTILITIES
# ============================================================================

def log(msg):
    print(f"[festiflow] {msg}", file=sys.stderr)


def log_error(msg):
    print(f"[festiflow] ❌ {msg}", file=sys.stderr)


def log_ok(msg):
    print(f"[festiflow] ✅ {msg}", file=sys.stderr)


def clean_price(price_str):
    """Clean price string to float: '€89.00' -> 89.0"""
    if not price_str or str(price_str).strip() == '':
        return 0.0
    cleaned = str(price_str).strip().replace('€', '').replace('$', '').replace(' ', '').replace(',', '')
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def cents_to_euros(cents):
    """Convert integer cents to float euros."""
    if cents is None:
        return 0.0
    return round(int(cents) / 100, 2)


def make_request(url, headers=None, data=None, method='GET'):
    """Make HTTP request with retry logic. Returns parsed JSON or None."""
    headers = headers or {}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if data is not None:
                req_data = json.dumps(data).encode('utf-8')
                headers['Content-Type'] = 'application/json'
            else:
                req_data = None

            req = urllib.request.Request(url, data=req_data, headers=headers, method=method)

            # Skip SSL verification for API calls (some environments need this)
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
                body = resp.read().decode('utf-8')
                return json.loads(body)

        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = RETRY_DELAY_SECONDS * attempt
                log(f"Rate limited (429). Waiting {wait}s... (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            elif e.code == 404:
                log_error(f"Not found (404): {url}")
                return None
            else:
                log_error(f"HTTP {e.code}: {e.reason} (attempt {attempt}/{MAX_RETRIES})")
                if attempt == MAX_RETRIES:
                    return None
                time.sleep(RETRY_DELAY_SECONDS)

        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log_error(f"Network error: {e} (attempt {attempt}/{MAX_RETRIES})")
            if attempt == MAX_RETRIES:
                return None
            time.sleep(RETRY_DELAY_SECONDS)

        except json.JSONDecodeError as e:
            log_error(f"Invalid JSON response: {e}")
            return None

    return None


# ============================================================================
# EVENT CONFIG LOADING
# ============================================================================

def load_event_config(config_path, event_id=None):
    """
    Load event configuration from CSV.
    Handles duplicate rows by merging (later rows override field by field).
    Returns dict for single event or dict of all events.
    """
    events = {}

    with open(config_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            eid = row['event_id']

            if eid not in events:
                events[eid] = {
                    'event_id': eid,
                    'event_name': row.get('event_name', ''),
                    'brand': row.get('brand', ''),
                    'venue': row.get('venue', ''),
                    'city': row.get('city', ''),
                    'currency': row.get('currency', 'EUR'),
                    'shotgun_event_id': row.get('shotgun_event_id', ''),
                    'dice_mio_id': row.get('dice_mio_id', ''),
                    'dice_public_id': row.get('dice_public_id', ''),
                    'compare_to': row.get('compare_to', ''),
                    'comparison_mode': row.get('comparison_mode', '').strip() or 'j_minus',
                    'status': row.get('status', 'archive'),
                    'output_filename': row.get('output_filename', ''),
                    'days': []
                }

            day_date_str = row.get('day_date', '')
            day_date = datetime.strptime(day_date_str, '%Y-%m-%d').date() if day_date_str else None
            day_capacity = int(row.get('day_capacity', 0)) if row.get('day_capacity', '').strip() else 0

            events[eid]['days'].append({
                'day_number': int(row.get('day_number', 1)),
                'day_name': row.get('day_name', ''),
                'day_date': day_date,
                'day_capacity': day_capacity,
            })

    # Derived fields
    for eid, event in events.items():
        event['days'].sort(key=lambda d: d['day_number'])
        dates = [d['day_date'] for d in event['days'] if d['day_date']]
        event['event_date_first'] = min(dates) if dates else None
        event['event_date_last'] = max(dates) if dates else None
        event['total_capacity'] = sum(d['day_capacity'] for d in event['days'])

    if event_id:
        return events.get(event_id)
    return events


# ============================================================================
# TICKET CLASSIFICATION (from run.py — same logic, same output)
# ============================================================================

def classify_ticket(name, price=None, tags='', is_dice_filename=False, event_days=None):
    """
    Universal ticket classifier. Identical to run.py's classify_ticket.
    Returns: (ticket_type, access_level, attendance_days, product_name)
    """
    if not name:
        return 'single_day', 'regular', [], ''

    raw = name.strip()
    n = raw.upper()

    if is_dice_filename:
        n = n.split('-DICE-')[0].split('-MADAME-LOYAL')[0].split('-SONORA')[0]
        n = n.replace('--', ' ').replace('-', ' ')

    n_clean = n
    for suffix in [' - JOUR 1', ' - JOUR 2', ' - JOUR 3', ' - DAY 1', ' - DAY 2', ' - DAY 3',
                   '(DERNIERS TICKETS)', '(OFFRE ULTRA LIMITÉE)', '(OFFRE ULTRA LIMITEE)']:
        n_clean = n_clean.replace(suffix, '')

    n_clean = re.sub(
        r'\d{1,2}\s+(JANVIER|FEVRIER|FÉVRIER|MARS|AVRIL|MAI|JUIN|JUILLET|AOUT|AOÛT|SEPTEMBRE|OCTOBRE|NOVEMBRE|DECEMBRE|DÉCEMBRE)',
        '', n_clean
    )

    # Access level
    access_level = 'regular'
    if tags and tags.strip().lower() == 'invitation':
        access_level = 'invitation'
    elif 'INVITATION' in n:
        access_level = 'invitation'
    elif 'JEU CONCOURS' in n:
        access_level = 'jeu_concours'
    elif 'VIP' in n or 'ACCÈS SCÈNE' in n or 'ACCES SCENE' in n:
        access_level = 'vip'
    elif 'GOLD' in n:
        access_level = 'vip'
    elif 'BACKSTAGE' in n and 'VIP' not in n:
        access_level = 'backstage'
    elif 'ENTRÉE AVANT' in n or 'ENTREE AVANT' in n:
        access_level = 'early_entry'
    elif '5 POUR 4' in n:
        access_level = 'group_discount'

    if price is not None and float(price) == 0 and access_level == 'regular':
        access_level = 'invitation'

    # Detect days mentioned
    days_found = []
    for day in ALL_DAYS:
        if day.upper() in n_clean:
            days_found.append(day)
    paren_match = re.search(r'\(([^)]+)\)', n_clean)
    if paren_match:
        for day in ALL_DAYS:
            if day.upper() in paren_match.group(1) and day not in days_found:
                days_found.append(day)
    days_found.sort(key=lambda d: ALL_DAYS.index(d))

    # Date-based fallback
    if not days_found and event_days:
        MONTHS_MAP = {
            'JANVIER': 1, 'FEVRIER': 2, 'FÉVRIER': 2, 'MARS': 3, 'AVRIL': 4, 'MAI': 5,
            'JUIN': 6, 'JUILLET': 7, 'AOUT': 8, 'AOÛT': 8, 'SEPTEMBRE': 9,
            'OCTOBRE': 10, 'NOVEMBRE': 11, 'DECEMBRE': 12, 'DÉCEMBRE': 12,
            'JAN': 1, 'FEV': 2, 'FÉV': 2, 'MAR': 3, 'AVR': 4, 'JUN': 6,
            'JUL': 7, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12, 'DÉC': 12,
        }
        date_match = re.search(
            r'(\d{1,2})\s+(JANVIER|FEVRIER|FÉVRIER|MARS|AVRIL|MAI|JUIN|JUILLET|AOUT|AOÛT|SEPTEMBRE|OCTOBRE|NOVEMBRE|DECEMBRE|DÉCEMBRE|JAN|FEV|FÉV|MAR|AVR|JUN|JUL|SEP|OCT|NOV|DEC|DÉC)',
            n
        )
        if date_match:
            day_num = int(date_match.group(1))
            month_num = MONTHS_MAP.get(date_match.group(2))
            if month_num:
                for ed in event_days:
                    dd = ed.get('day_date')
                    if dd and dd.day == day_num and dd.month == month_num:
                        days_found.append(ed['day_name'].lower())
                        break

    # Ticket type
    ticket_type = None
    attendance_days = []

    if '3 JOURS' in n_clean or 'TROIS JOURS' in n_clean:
        ticket_type = '3-jours'
        attendance_days = days_found if len(days_found) >= 3 else None
    elif '2 JOURS' in n_clean or 'DEUX JOURS' in n_clean:
        ticket_type = '2-jours'
        attendance_days = days_found if len(days_found) >= 2 else None
    elif '1 JOUR' in n_clean:
        ticket_type = 'single_day'
        attendance_days = days_found if days_found else None
    elif len(days_found) >= 3:
        ticket_type = '3-jours'
        attendance_days = days_found
    elif len(days_found) == 2:
        ticket_type = '2-jours'
        attendance_days = days_found
    elif len(days_found) == 1:
        ticket_type = days_found[0]
        attendance_days = days_found
    else:
        ticket_type = 'single_day'
        attendance_days = None

    # Product name
    if is_dice_filename:
        product_name = raw.split('-DICE-')[0].split('-madame-loyal')[0].split('-sonora')[0]
        product_name = product_name.replace('--', ' + ').replace('-', ' ').strip().title()
    else:
        product_name = raw.strip()
        if product_name.isupper():
            product_name = product_name.title()

    return ticket_type, access_level, attendance_days, product_name


def resolve_attendance(ticket_type, attendance_days, event_day_names):
    """
    Resolve attendance_days into a concrete presence dict.
    Identical to run.py's resolve_attendance.
    Returns: dict {day_name: 1 or 0}
    """
    presence = {dn: 0 for dn in event_day_names}

    if attendance_days is not None and len(attendance_days) > 0:
        for d in attendance_days:
            if d in presence:
                presence[d] = 1
    elif ticket_type == '3-jours':
        for dn in event_day_names:
            presence[dn] = 1
    elif ticket_type == '2-jours':
        main_days = event_day_names[-2:] if len(event_day_names) >= 2 else event_day_names
        for dn in main_days:
            presence[dn] = 1
    elif ticket_type in event_day_names:
        presence[ticket_type] = 1
    elif ticket_type == 'single_day':
        for dn in event_day_names:
            presence[dn] = 1

    return presence


# ============================================================================
# SHOTGUN API FETCH
# ============================================================================

def fetch_shotgun_tickets(shotgun_event_id, token, organizer_id, event_days=None):
    """
    Fetch all tickets for an event from Shotgun REST API.
    Paginates through all pages (100 per page).

    Returns: (list of normalized ticket dicts, source_info dict)
    """
    if not shotgun_event_id or not token:
        return [], {'status': 'skipped', 'tickets_fetched': 0, 'event_id': shotgun_event_id}

    log(f"Fetching Shotgun tickets for event {shotgun_event_id}...")
    started = datetime.utcnow()
    all_tickets = []
    errors = []
    null_price_count = 0

    url = (
        f"{SHOTGUN_API_BASE}/tickets"
        f"?token={urllib.parse.quote(token)}"
        f"&organizer_id={organizer_id}"
        f"&event_id={shotgun_event_id}"
    )

    page = 0
    while url:
        page += 1
        log(f"  Shotgun page {page}...")
        resp = make_request(url)

        if resp is None:
            return all_tickets, {
                'status': 'error',
                'error': 'API request failed after retries',
                'tickets_fetched': len(all_tickets),
                'event_id': shotgun_event_id,
                'fetched_at': datetime.utcnow().isoformat() + 'Z'
            }

        tickets_data = resp.get('data', [])

        for t in tickets_data:
            # Skip non-valid tickets
            status = t.get('ticket_status', '')
            if status not in ('valid', 'resold'):
                continue

            # Parse dates
            ordered_at = t.get('ordered_at', '')
            order_date = ordered_at[:10] if ordered_at else None
            order_datetime = ordered_at

            if not order_date:
                continue

            # Prices (API returns cents)
            deal_price = cents_to_euros(t.get('deal_price', 0))
            deal_service_fee = cents_to_euros(t.get('deal_service_fee', 0))
            deal_user_service_fee = cents_to_euros(t.get('deal_user_service_fee', 0))
            deal_producer_cost = cents_to_euros(t.get('deal_producer_cost', 0))
            vat_rate = t.get('deal_vat_rate', 0) or 0

            # Gross = what buyer paid for ticket face value
            gross_price = deal_price + deal_user_service_fee
            # Fees
            fees_organizer = deal_service_fee
            fees_platform = 0.0  # Shotgun doesn't charge a separate platform fee on the ticket
            fees_user = deal_user_service_fee
            # VAT
            vat_amount = round(deal_price * float(vat_rate), 2) if vat_rate else 0.0

            if deal_price == 0 and deal_user_service_fee == 0:
                null_price_count += 1

            # Classify
            deal_title = t.get('deal_title', '')
            deal_sub_category = t.get('deal_sub_category', '')
            deal_channel = t.get('deal_channel', '')
            combined = f"{deal_sub_category} {deal_title}".strip()

            ticket_type, access_level, attendance_days, product_name = classify_ticket(
                combined, price=gross_price, tags='invitation' if deal_channel == 'invitation' else '',
                event_days=event_days
            )

            if deal_sub_category and deal_sub_category.strip():
                product_name = deal_sub_category.strip()
                if product_name.isupper():
                    product_name = product_name.title()

            # Day fallback from event start/end times
            if (attendance_days is None or len(attendance_days) == 0) and ticket_type == 'single_day':
                event_start = t.get('event_start_time', '')
                if event_start:
                    try:
                        start_date = datetime.fromisoformat(event_start.replace('Z', '+00:00'))
                        day_map = {0: 'lundi', 1: 'mardi', 2: 'mercredi', 3: 'jeudi',
                                   4: 'vendredi', 5: 'samedi', 6: 'dimanche'}
                        day_from_date = day_map.get(start_date.weekday())
                        if day_from_date:
                            ticket_type = day_from_date
                            attendance_days = [day_from_date]
                    except (ValueError, AttributeError):
                        pass

            is_paid = 0 if access_level in ('invitation', 'jeu_concours') else 1
            if deal_price == 0 and access_level == 'regular':
                access_level = 'invitation'
                is_paid = 0

            all_tickets.append({
                'order_date': order_date,
                'order_datetime': order_datetime,
                'ticket_type': ticket_type,
                'access_level': access_level,
                'attendance_days': attendance_days,
                'product_name': product_name,
                'label': deal_title,
                'platform': 'shotgun',
                'gross_price': gross_price,
                'fees_organizer': fees_organizer,
                'fees_platform': fees_platform,
                'fees_user': fees_user,
                'vat': vat_amount,
                'is_paid': is_paid,
            })

        # Pagination
        next_url = resp.get('pagination', {}).get('next')
        url = next_url if next_url else None

    if null_price_count > 0:
        errors.append(f"{null_price_count} tickets with null price on shotgun")

    log_ok(f"Shotgun: {len(all_tickets)} valid tickets fetched")

    return all_tickets, {
        'status': 'ok',
        'tickets_fetched': len(all_tickets),
        'event_id': int(shotgun_event_id) if shotgun_event_id.isdigit() else shotgun_event_id,
        'fetched_at': datetime.utcnow().isoformat() + 'Z',
        'errors': errors
    }


# ============================================================================
# DICE GRAPHQL FETCH
# ============================================================================

DICE_ORDERS_QUERY = """
query FetchOrders($eventId: ID!, $first: Int!, $after: String) {
  node(id: $eventId) {
    ... on Event {
      name
      startDatetime
      endDatetime
      tickets(first: $first, after: $after) {
        totalCount
        pageInfo {
          endCursor
          hasNextPage
        }
        edges {
          node {
            id
            code
            fullPrice
            commission
            diceCommission
            total
            fees {
              category
              amount
            }
            ticketType {
              name
              description
            }
            priceTier {
              name
              type
            }
            claimedAt
          }
        }
      }
    }
  }
}
"""

# Separate query to get purchase dates from orders
DICE_ORDERS_WITH_DATES_QUERY = """
query FetchOrdersWithDates($first: Int!, $after: String) {
  viewer {
    orders(first: $first, after: $after) {
      pageInfo {
        endCursor
        hasNextPage
      }
      edges {
        node {
          id
          purchasedAt
          quantity
          salesChannel
          tickets {
            id
            code
            fullPrice
            commission
            diceCommission
            total
            fees {
              category
              amount
            }
            ticketType {
              name
              description
            }
            priceTier {
              name
              type
            }
          }
        }
      }
    }
  }
}
"""


def fetch_dice_tickets(dice_event_id, token, event_days=None):
    """
    Fetch all tickets for an event from DICE GraphQL API.
    Uses the node(id) query for event tickets, then orders query for purchase dates.

    Returns: (list of normalized ticket dicts, source_info dict)
    """
    if not dice_event_id or not token:
        return [], {'status': 'skipped', 'tickets_fetched': 0, 'event_id': dice_event_id}

    log(f"Fetching DICE tickets for event {dice_event_id}...")
    started = datetime.utcnow()
    all_tickets = []
    errors = []
    null_price_count = 0

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }

    # Step 1: Fetch all orders to get purchasedAt + ticket details
    # We use the viewer.orders approach since tickets don't have purchasedAt
    # We'll filter to our event client-side via ticket matching
    log("  DICE: Fetching orders with purchase dates...")

    # First, get event info (name, to verify we have the right event)
    event_info_query = """
    query EventInfo($id: ID!) {
      node(id: $id) {
        ... on Event {
          name
          startDatetime
          endDatetime
          ticketTypes { name id }
        }
      }
    }
    """
    event_resp = make_request(DICE_GRAPHQL_ENDPOINT, headers=headers, data={
        'query': event_info_query,
        'variables': {'id': str(dice_event_id)}
    }, method='POST')

    if event_resp is None or 'errors' in event_resp:
        err_msg = 'API request failed'
        if event_resp and 'errors' in event_resp:
            err_msg = str(event_resp['errors'][0].get('message', err_msg))
        return [], {
            'status': 'error',
            'error': err_msg,
            'tickets_fetched': 0,
            'event_id': str(dice_event_id),
            'fetched_at': datetime.utcnow().isoformat() + 'Z'
        }

    event_node = event_resp.get('data', {}).get('node')
    if not event_node:
        return [], {
            'status': 'error',
            'error': f'Event {dice_event_id} not found',
            'tickets_fetched': 0,
            'event_id': str(dice_event_id),
            'fetched_at': datetime.utcnow().isoformat() + 'Z'
        }

    dice_event_name = event_node.get('name', '')
    dice_ticket_type_ids = {tt['id'] for tt in event_node.get('ticketTypes', [])}
    log(f"  DICE event: {dice_event_name}")
    log(f"  DICE ticket types: {len(dice_ticket_type_ids)}")

    # Step 2: Fetch tickets via event node (has fee breakdown but no purchasedAt)
    cursor = None
    page = 0
    ticket_map = {}  # code -> ticket data

    while True:
        page += 1
        log(f"  DICE tickets page {page}...")
        variables = {'eventId': str(dice_event_id), 'first': 50}
        if cursor:
            variables['after'] = cursor

        resp = make_request(DICE_GRAPHQL_ENDPOINT, headers=headers, data={
            'query': DICE_ORDERS_QUERY,
            'variables': variables
        }, method='POST')

        if resp is None or 'errors' in resp:
            err_msg = 'Ticket fetch failed'
            if resp and 'errors' in resp:
                err_msg = str(resp['errors'][0].get('message', err_msg))
            errors.append(err_msg)
            break

        event_data = resp.get('data', {}).get('node', {})
        tickets_conn = event_data.get('tickets', {})
        edges = tickets_conn.get('edges', [])

        for edge in edges:
            node = edge.get('node', {})
            code = node.get('code', '')
            ticket_map[code] = {
                'id': node.get('id'),
                'code': code,
                'fullPrice': cents_to_euros(node.get('fullPrice', 0)),
                'commission': cents_to_euros(node.get('commission', 0)),
                'diceCommission': cents_to_euros(node.get('diceCommission', 0)),
                'total': cents_to_euros(node.get('total', 0)),
                'fees': node.get('fees', []),
                'ticketType': node.get('ticketType', {}),
                'priceTier': node.get('priceTier'),
                'claimedAt': node.get('claimedAt'),
            }

        page_info = tickets_conn.get('pageInfo', {})
        if page_info.get('hasNextPage'):
            cursor = page_info.get('endCursor')
        else:
            break

    log(f"  DICE: {len(ticket_map)} tickets from event node")

    # Step 3: Fetch orders for purchase dates
    # Query viewer.orders and match to our event's tickets
    log("  DICE: Fetching orders for purchase dates...")
    order_cursor = None
    order_page = 0
    purchase_dates = {}  # ticket_code -> purchasedAt
    total_orders_scanned = 0

    while True:
        order_page += 1
        log(f"  DICE orders page {order_page}...")
        variables = {'first': 50}
        if order_cursor:
            variables['after'] = order_cursor

        resp = make_request(DICE_GRAPHQL_ENDPOINT, headers=headers, data={
            'query': DICE_ORDERS_WITH_DATES_QUERY,
            'variables': variables
        }, method='POST')

        if resp is None or 'errors' in resp:
            err_msg = 'Order fetch failed'
            if resp and 'errors' in resp:
                err_msg = str(resp['errors'][0].get('message', err_msg))
            errors.append(err_msg)
            break

        viewer = resp.get('data', {}).get('viewer', {})
        orders_conn = viewer.get('orders', {})
        edges = orders_conn.get('edges', [])

        if not edges:
            break

        for edge in edges:
            order = edge.get('node', {})
            purchased_at = order.get('purchasedAt', '')
            order_tickets = order.get('tickets', [])

            for ot in order_tickets:
                code = ot.get('code', '')
                if code in ticket_map:
                    purchase_dates[code] = purchased_at

        total_orders_scanned += len(edges)

        page_info = orders_conn.get('pageInfo', {})
        if page_info.get('hasNextPage'):
            order_cursor = page_info.get('endCursor')
        else:
            break

        # Early exit if we've found dates for all our tickets
        if len(purchase_dates) >= len(ticket_map):
            break

    log(f"  DICE: matched {len(purchase_dates)}/{len(ticket_map)} purchase dates from {total_orders_scanned} orders")

    # Step 4: Normalize tickets
    for code, t in ticket_map.items():
        purchased_at = purchase_dates.get(code, t.get('claimedAt', ''))
        order_date = purchased_at[:10] if purchased_at else None

        if not order_date:
            # Use claimedAt as fallback
            claimed = t.get('claimedAt', '')
            order_date = claimed[:10] if claimed else None

        if not order_date:
            null_price_count += 1
            continue

        # Prices
        full_price = t['fullPrice']  # face value without commissions
        commission = t['commission']  # partner (organizer) commission
        dice_commission = t['diceCommission']  # DICE platform commission
        total = t['total']  # what buyer paid

        # Fee breakdown from fees array
        vat_amount = 0.0
        for fee in t.get('fees', []):
            cat = (fee.get('category') or '').upper()
            amount = cents_to_euros(fee.get('amount', 0))
            if cat == 'SALES_TAX' or 'TAX' in cat or 'TVA' in cat:
                vat_amount += amount

        gross_price = total  # total is what buyer paid (face + fees)
        fees_organizer = commission
        fees_platform = dice_commission
        fees_user = total - full_price - commission - dice_commission
        if fees_user < 0:
            fees_user = 0.0

        # Classify
        ticket_type_name = t.get('ticketType', {}).get('name', '')
        ticket_type, access_level, attendance_days, product_name = classify_ticket(
            ticket_type_name, price=total, event_days=event_days
        )

        is_paid = 0 if access_level in ('invitation', 'jeu_concours') else 1
        if total == 0 and access_level == 'regular':
            access_level = 'invitation'
            is_paid = 0

        all_tickets.append({
            'order_date': order_date,
            'order_datetime': purchased_at or '',
            'ticket_type': ticket_type,
            'access_level': access_level,
            'attendance_days': attendance_days,
            'product_name': product_name,
            'label': ticket_type_name,
            'platform': 'dice',
            'gross_price': round(gross_price, 2),
            'fees_organizer': round(fees_organizer, 2),
            'fees_platform': round(fees_platform, 2),
            'fees_user': round(fees_user, 2),
            'vat': round(vat_amount, 2),
            'is_paid': is_paid,
        })

    if null_price_count > 0:
        errors.append(f"{null_price_count} tickets with null date on dice")

    log_ok(f"DICE: {len(all_tickets)} valid tickets normalized")

    return all_tickets, {
        'status': 'ok' if not errors else 'partial',
        'tickets_fetched': len(all_tickets),
        'event_id': str(dice_event_id),
        'fetched_at': datetime.utcnow().isoformat() + 'Z',
        'errors': errors
    }


# ============================================================================
# AGGREGATION
# ============================================================================

def empty_fee_bucket():
    return {
        'gross_revenue': 0.0,
        'fees_organizer': 0.0,
        'fees_platform': 0.0,
        'fees_user': 0.0,
        'vat': 0.0,
    }


def add_fees(target, ticket):
    """Accumulate fee fields from a ticket into a target dict."""
    target['gross_revenue'] = round(target['gross_revenue'] + ticket['gross_price'], 2)
    target['fees_organizer'] = round(target['fees_organizer'] + ticket['fees_organizer'], 2)
    target['fees_platform'] = round(target['fees_platform'] + ticket['fees_platform'], 2)
    target['fees_user'] = round(target['fees_user'] + ticket['fees_user'], 2)
    target['vat'] = round(target['vat'] + ticket['vat'], 2)


def aggregate(tickets, event_config):
    """
    Compute the full JSON schema from a list of normalized tickets.
    Every breakdown level includes gross_revenue + all 4 fee/VAT fields.
    """
    event_day_names = [d['day_name'].lower() for d in event_config['days']]
    event_days_info = event_config['days']

    # === TOTALS ===
    totals = {
        'tickets_sold': 0,
        'tickets_paid': 0,
        'tickets_unpaid': 0,
        **empty_fee_bucket(),
        'net_revenue': 0.0,
        'capacity_total': event_config['total_capacity'],
        'fill_rate': 0.0,
    }

    # === BY TICKET TYPE ===
    type_buckets = defaultdict(lambda: {
        'label': '', 'ticket_type': '', 'access_level': '',
        'count': 0, 'count_paid': 0, 'count_unpaid': 0,
        **empty_fee_bucket(), 'avg_price': 0.0, '_price_sum': 0.0
    })

    # === BY PLATFORM ===
    plat_buckets = defaultdict(lambda: {
        'platform': '', 'count': 0, **empty_fee_bucket()
    })

    # === BY DAY ===
    day_buckets = {}
    for d in event_days_info:
        dn = d['day_name'].lower()
        day_buckets[dn] = {
            'day': d['day_name'], 'date': d['day_date'].isoformat() if d['day_date'] else '',
            'count': 0, 'count_paid': 0, 'count_unpaid': 0,
            'capacity': d['day_capacity'],
            'fill_rate': 0.0, **empty_fee_bucket()
        }

    # === BY ACCESS LEVEL ===
    access_buckets = defaultdict(lambda: {
        'level': '', 'count': 0, 'count_paid': 0, 'count_unpaid': 0,
        **empty_fee_bucket()
    })

    # === BY TICKET TYPE × DAY (finest grain) ===
    type_day_buckets = defaultdict(lambda: {
        'day': '', 'label': '', 'ticket_type': '', 'access_level': '',
        'count': 0, 'count_paid': 0, 'count_unpaid': 0,
        **empty_fee_bucket()
    })

    # === VELOCITY ===
    today = date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday of current week
    prev_week_start = week_start - timedelta(days=7)

    current_week = {'tickets': 0, 'gross_revenue': 0.0}
    previous_week = {'tickets': 0, 'gross_revenue': 0.0}

    # Process each ticket
    for t in tickets:
        is_paid = t['is_paid']

        # Totals
        totals['tickets_sold'] += 1
        if is_paid:
            totals['tickets_paid'] += 1
        else:
            totals['tickets_unpaid'] += 1
        add_fees(totals, t)

        # By ticket type
        type_key = f"{t['ticket_type']}|{t['access_level']}"
        tb = type_buckets[type_key]
        tb['label'] = t['product_name']
        tb['ticket_type'] = t['ticket_type']
        tb['access_level'] = t['access_level']
        tb['count'] += 1
        if is_paid:
            tb['count_paid'] += 1
        else:
            tb['count_unpaid'] += 1
        add_fees(tb, t)
        tb['_price_sum'] += t['gross_price']

        # By platform
        pb = plat_buckets[t['platform']]
        pb['platform'] = t['platform']
        pb['count'] += 1
        add_fees(pb, t)

        # By access level
        ab = access_buckets[t['access_level']]
        ab['level'] = t['access_level']
        ab['count'] += 1
        if is_paid:
            ab['count_paid'] += 1
        else:
            ab['count_unpaid'] += 1
        add_fees(ab, t)

        # Resolve attendance for day-based breakdowns
        presence = resolve_attendance(t['ticket_type'], t['attendance_days'], event_day_names)
        days_present = [d for d, v in presence.items() if v == 1]
        num_days_present = len(days_present) or 1

        # Split revenue evenly across days for multi-day passes
        day_share = {
            'gross_price': round(t['gross_price'] / num_days_present, 2),
            'fees_organizer': round(t['fees_organizer'] / num_days_present, 2),
            'fees_platform': round(t['fees_platform'] / num_days_present, 2),
            'fees_user': round(t['fees_user'] / num_days_present, 2),
            'vat': round(t['vat'] / num_days_present, 2),
        }

        for day_name in days_present:
            if day_name in day_buckets:
                db = day_buckets[day_name]
                db['count'] += 1
                if is_paid:
                    db['count_paid'] += 1
                else:
                    db['count_unpaid'] += 1
                db['gross_revenue'] = round(db['gross_revenue'] + day_share['gross_price'], 2)
                db['fees_organizer'] = round(db['fees_organizer'] + day_share['fees_organizer'], 2)
                db['fees_platform'] = round(db['fees_platform'] + day_share['fees_platform'], 2)
                db['fees_user'] = round(db['fees_user'] + day_share['fees_user'], 2)
                db['vat'] = round(db['vat'] + day_share['vat'], 2)

            # Type × day
            td_key = f"{t['ticket_type']}|{t['access_level']}|{day_name}"
            tdb = type_day_buckets[td_key]
            tdb['day'] = day_name.capitalize()
            tdb['label'] = t['product_name']
            tdb['ticket_type'] = t['ticket_type']
            tdb['access_level'] = t['access_level']
            tdb['count'] += 1
            if is_paid:
                tdb['count_paid'] += 1
            else:
                tdb['count_unpaid'] += 1
            tdb['gross_revenue'] = round(tdb['gross_revenue'] + day_share['gross_price'], 2)
            tdb['fees_organizer'] = round(tdb['fees_organizer'] + day_share['fees_organizer'], 2)
            tdb['fees_platform'] = round(tdb['fees_platform'] + day_share['fees_platform'], 2)
            tdb['fees_user'] = round(tdb['fees_user'] + day_share['fees_user'], 2)
            tdb['vat'] = round(tdb['vat'] + day_share['vat'], 2)

        # Velocity
        try:
            t_date = date.fromisoformat(t['order_date'])
            if week_start <= t_date < week_start + timedelta(days=7):
                current_week['tickets'] += 1
                current_week['gross_revenue'] = round(current_week['gross_revenue'] + t['gross_price'], 2)
            elif prev_week_start <= t_date < week_start:
                previous_week['tickets'] += 1
                previous_week['gross_revenue'] = round(previous_week['gross_revenue'] + t['gross_price'], 2)
        except (ValueError, TypeError):
            pass

    # Compute derived fields
    if totals['capacity_total'] > 0:
        totals['fill_rate'] = round(totals['tickets_sold'] / totals['capacity_total'], 4)
    totals['net_revenue'] = round(
        totals['gross_revenue'] - totals['fees_platform'], 2
    )

    # Avg price per ticket type
    for tb in type_buckets.values():
        if tb['count'] > 0:
            tb['avg_price'] = round(tb['_price_sum'] / tb['count'], 2)
        del tb['_price_sum']

    # Day fill rates
    for db in day_buckets.values():
        if db['capacity'] > 0:
            db['fill_rate'] = round(db['count'] / db['capacity'], 4)

    # Velocity delta
    prev_tickets = previous_week['tickets'] if previous_week['tickets'] > 0 else None
    if prev_tickets:
        wow_delta = round((current_week['tickets'] - prev_tickets) / prev_tickets, 4)
    else:
        wow_delta = None

    # Week numbers
    current_week_num = (today - event_config.get('event_date_first', today)).days // 7 + 1

    return {
        'totals': totals,
        'by_ticket_type': sorted(type_buckets.values(), key=lambda x: x['count'], reverse=True),
        'by_platform': list(plat_buckets.values()),
        'by_day': [day_buckets[d['day_name'].lower()] for d in event_days_info if d['day_name'].lower() in day_buckets],
        'by_access_level': sorted(access_buckets.values(), key=lambda x: x['count'], reverse=True),
        'by_ticket_type_and_day': sorted(type_day_buckets.values(), key=lambda x: (x['day'], -x['count'])),
        'velocity': {
            'current_week': {
                'week_number': current_week_num,
                'tickets': current_week['tickets'],
                'gross_revenue': current_week['gross_revenue'],
            },
            'previous_week': {
                'week_number': current_week_num - 1,
                'tickets': previous_week['tickets'],
                'gross_revenue': previous_week['gross_revenue'],
            },
            'week_over_week_delta': wow_delta,
        }
    }


# ============================================================================
# COMPARISON / REFERENCE DATA
# ============================================================================

def resolve_reference(compare_to, config_path, csv_database_dir):
    """
    Resolve reference event data. Tries LIVE API fetch first (if the reference
    event has API credentials), falls back to stored CSV.

    Returns: (reference_tickets list, ref_config dict, source "live"|"stored") or (None, None, None)
    """
    if not compare_to:
        return None, None, None

    ref_config = load_event_config(config_path, event_id=compare_to)
    if not ref_config:
        log(f"Reference event '{compare_to}' not found in config")
        return None, None, None

    # Try LIVE fetch if API credentials exist
    has_shotgun = bool(ref_config.get('shotgun_event_id', '').strip())
    has_dice = bool(ref_config.get('dice_mio_id', '').strip())

    if has_shotgun or has_dice:
        log(f"Reference '{compare_to}': live-fetching (shotgun={has_shotgun}, dice={has_dice})")

        shotgun_token = os.environ.get('SHOTGUN_TOKEN', '')
        shotgun_org_id = os.environ.get('SHOTGUN_ORGANIZER_ID', DEFAULT_ORGANIZER_ID)
        dice_token = os.environ.get('DICE_TOKEN', '')
        ref_days = ref_config['days']

        ref_tickets = []

        if has_shotgun and shotgun_token:
            sg_tickets, sg_source = fetch_shotgun_tickets(
                ref_config['shotgun_event_id'], shotgun_token, shotgun_org_id,
                event_days=ref_days
            )
            ref_tickets.extend(sg_tickets)
            log(f"  Reference Shotgun: {sg_source['tickets_fetched']} tickets ({sg_source['status']})")

        if has_dice and dice_token:
            dice_tickets, dice_source = fetch_dice_tickets(
                ref_config['dice_mio_id'], dice_token,
                event_days=ref_days
            )
            ref_tickets.extend(dice_tickets)
            log(f"  Reference DICE: {dice_source['tickets_fetched']} tickets ({dice_source['status']})")

        if ref_tickets:
            ref_tickets.sort(key=lambda t: t.get('order_date', ''))
            log_ok(f"Reference '{compare_to}': {len(ref_tickets)} tickets fetched live")
            return ref_tickets, ref_config, 'live'
        else:
            log(f"Reference '{compare_to}': live fetch returned 0 tickets, trying stored CSV")

    # Fall back to stored CSV (e.g. EPK 2023 coproduction case)
    merged_path = Path(csv_database_dir) / compare_to / f"{compare_to}_merged.csv"
    if merged_path.exists():
        log(f"Reference '{compare_to}': loading from stored CSV {merged_path}")
        tickets = []
        with open(merged_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                tickets.append({
                    'order_date': row.get('order_date', ''),
                    'gross_price': clean_price(row.get('gross_price', '0')),
                    'fees_organizer': clean_price(row.get('fees_organizer', '0')),
                    'fees_platform': clean_price(row.get('fees_platform', '0')),
                    'fees_user': clean_price(row.get('fees_user', '0')),
                    'vat': clean_price(row.get('vat', '0')),
                    'is_paid': int(row.get('is_paid', 1)),
                })
        log_ok(f"Reference '{compare_to}': {len(tickets)} tickets from stored CSV")
        return tickets, ref_config, 'stored'

    log(f"Reference '{compare_to}': no API credentials and no stored CSV — comparison unavailable")
    return None, None, None


def compute_comparison(current_tickets, reference_tickets, event_config, ref_config, ref_source):
    """
    Compute comparison metrics between current event and reference event.
    Implements j_minus time windowing: filters reference tickets to the
    equivalent point in the reference event's lifecycle.
    """
    if not reference_tickets or not ref_config:
        return None

    compare_to = event_config.get('compare_to', '')
    today = date.today()

    event_last = event_config.get('event_date_last')
    ref_last = ref_config.get('event_date_last')
    if not event_last or not ref_last:
        return None

    # Time windowing: how many days until current event?
    days_until = (event_last - today).days

    if days_until <= 0:
        # Current event is past — use all reference tickets (final vs final)
        ref_filtered = reference_tickets
        ref_snapshot = ref_last.isoformat()
    else:
        # Current event is upcoming — find the equivalent cutoff in reference year
        ref_cutoff = ref_last - timedelta(days=days_until)
        ref_filtered = [t for t in reference_tickets if t.get('order_date', '') <= ref_cutoff.isoformat()]
        ref_snapshot = ref_cutoff.isoformat()

    ref_total_tickets = len(ref_filtered)
    ref_total_revenue = round(sum(t.get('gross_price', 0) for t in ref_filtered), 2)

    curr_total_tickets = len(current_tickets)
    curr_total_revenue = round(sum(t.get('gross_price', 0) for t in current_tickets), 2)

    delta_tickets = round((curr_total_tickets - ref_total_tickets) / ref_total_tickets, 4) if ref_total_tickets > 0 else None
    delta_revenue = round((curr_total_revenue - ref_total_revenue) / ref_total_revenue, 4) if ref_total_revenue > 0 else None

    return {
        'reference_event': compare_to,
        'reference_source': ref_source,
        'reference_snapshot_date': ref_snapshot,
        'reference_at_same_point': {
            'tickets_sold': ref_total_tickets,
            'gross_revenue': ref_total_revenue,
        },
        'delta_tickets': delta_tickets,
        'delta_revenue': delta_revenue,
    }


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def fetch_event(event_id, config_path='event_config.csv', csv_database='csv_database'):
    """
    Main entry point. Fetches live data, classifies, aggregates, returns JSON dict.
    """
    # Load config
    event_config = load_event_config(config_path, event_id=event_id)
    if not event_config:
        return {'meta': {'status': 'error', 'errors': [f'Event {event_id} not found in config']}}

    event_days = event_config['days']
    event_day_names = [d['day_name'].lower() for d in event_days]

    # Tokens from environment
    shotgun_token = os.environ.get('SHOTGUN_TOKEN', '')
    shotgun_org_id = os.environ.get('SHOTGUN_ORGANIZER_ID', DEFAULT_ORGANIZER_ID)
    dice_token = os.environ.get('DICE_TOKEN', '')

    # Fetch from both platforms
    shotgun_tickets, shotgun_source = fetch_shotgun_tickets(
        event_config.get('shotgun_event_id', ''),
        shotgun_token,
        shotgun_org_id,
        event_days=event_days
    )

    dice_tickets, dice_source = fetch_dice_tickets(
        event_config.get('dice_mio_id', ''),
        dice_token,
        event_days=event_days
    )

    # Merge
    all_tickets = shotgun_tickets + dice_tickets
    all_tickets.sort(key=lambda t: t.get('order_date', ''))

    # Determine overall status
    all_errors = []
    shotgun_errors = shotgun_source.get('errors', [])
    dice_errors = dice_source.get('errors', [])
    if isinstance(shotgun_errors, list):
        all_errors.extend(shotgun_errors)
    if isinstance(dice_errors, list):
        all_errors.extend(dice_errors)

    if shotgun_source['status'] == 'error' and dice_source['status'] == 'error':
        overall_status = 'error'
    elif shotgun_source['status'] == 'error' or dice_source['status'] == 'error':
        overall_status = 'partial'
    else:
        overall_status = 'ok'

    # Aggregate
    aggregated = aggregate(all_tickets, event_config)

    # Comparison (live fetch first, stored CSV fallback)
    ref_tickets, ref_config, ref_source = resolve_reference(
        event_config.get('compare_to', ''),
        config_path,
        csv_database
    )
    comparison = compute_comparison(all_tickets, ref_tickets, event_config, ref_config, ref_source)

    # Build final JSON
    result = {
        'meta': {
            'event_id': event_id,
            'event_name': event_config.get('event_name', ''),
            'generated_at': datetime.utcnow().isoformat() + 'Z',
            'status': overall_status,
            'errors': all_errors,
            'version': VERSION,
            'sources': {
                'shotgun': {k: v for k, v in shotgun_source.items() if k != 'errors'},
                'dice': {k: v for k, v in dice_source.items() if k != 'errors'},
            }
        },
        'event': {
            'days': [
                {
                    'name': d['day_name'],
                    'date': d['day_date'].isoformat() if d['day_date'] else None,
                    'capacity': d['day_capacity']
                }
                for d in event_days
            ],
            'currency': event_config.get('currency', 'EUR'),
            'compare_to': event_config.get('compare_to', ''),
            'comparison_mode': event_config.get('comparison_mode', 'j_minus'),
        },
        **aggregated,
    }

    if comparison:
        result['comparison'] = comparison

    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Festiflow live ticket data fetch')
    parser.add_argument('event_id', nargs='?', help='Event ID (e.g. epk_2026)')
    parser.add_argument('--all-active', action='store_true', help='Fetch all active events')
    parser.add_argument('--config', default='event_config.csv', help='Path to event_config.csv')
    parser.add_argument('--csv-database', default='csv_database', help='Path to csv_database dir')
    parser.add_argument('--output', '-o', help='Output file path (default: stdout)')
    parser.add_argument('--pretty', action='store_true', help='Pretty-print JSON')
    args = parser.parse_args()

    if not args.event_id and not args.all_active:
        parser.error('Provide an event_id or --all-active')

    if args.all_active:
        all_events = load_event_config(args.config)
        active_ids = [eid for eid, ec in all_events.items() if ec.get('status') == 'active']
        log(f"Fetching {len(active_ids)} active events: {active_ids}")
        results = {}
        for eid in active_ids:
            results[eid] = fetch_event(eid, config_path=args.config, csv_database=args.csv_database)
        output = results
    else:
        output = fetch_event(args.event_id, config_path=args.config, csv_database=args.csv_database)

    indent = 2 if args.pretty else None
    json_str = json.dumps(output, indent=indent, ensure_ascii=False, default=str)

    if args.output:
        Path(args.output).write_text(json_str, encoding='utf-8')
        log_ok(f"Written to {args.output}")
    else:
        print(json_str)


if __name__ == '__main__':
    main()
