/* ============================================================================
 * Module Dates — mock-data.js
 * ----------------------------------------------------------------------------
 * Frontend-only V1 mock data. Mirrors `docs/architecture/SCHEMA.md` exactly.
 * Every mock function returns a Promise (mirrors eventual fetch() shape).
 *
 * Swap at BudgetFlow V1 backend-wire ship: replace each function body with a
 * fetch() call. Render functions and handlers consume Promises, unchanged.
 *
 * Discipline anchors:
 * - SCHEMA §2.1 — UUIDs (server-generated UUID v4 in prod; hardcoded shape in mock)
 * - SCHEMA §2.4 — naming + enum storage convention (UPPERCASE_SNAKE_CASE in storage)
 * - SCHEMA §2.5 — is_sandbox is the canonical sandbox-flag column
 * - SCHEMA §3.1 — users
 * - SCHEMA §4.1 — qonto_accounts
 * - SCHEMA §4.2 — coproducers
 * - SCHEMA §4.3 — event_series
 * - SCHEMA §4.4 — events (26 cols)
 * - SCHEMA §4.5 — event_days
 *
 * Carry-pending decisions surfaced by this mock data (see DESIGN_LOG.md):
 * - #1 events.status enum: PREP / VALIDÉ / LAUNCHED / LIVE / CLOSED (5 vals)
 *      → SANDBOX dropped from enum; is_sandbox boolean is the sandbox flag
 *        (resolves #2). VALIDÉ added per brief §11 line 119.
 * - #2 boolean wins over status='SANDBOX' enum coupling (see #1).
 * - #5 read contract: FLAT shape. getEvent(id) returns events-row columns
 *      only; coproducer/qonto/series names rendered via separate cached calls.
 * ========================================================================= */

'use strict';

/* ----------------------------------------------------------------------------
 * Hardcoded constants — UUID-shape strings for deterministic mock behavior
 * ------------------------------------------------------------------------- */

const CURRENT_USER = {
  id: '00000000-0000-4000-8000-000000000001',
  email: 'leonard@ai2k.dev',
  name: 'Leo',
  google_user_id: 'mock-google-sub-leo-001',
  role: 'admin',          // PLACEHOLDER — vocab pending Module Admin brief (SCHEMA §3.1 Notes)
  status: 'ACTIVE',       // stable-vocab Option C per SCHEMA §2.4
  last_login_at: '2026-05-15T09:00:00Z',
};

// Status enum vocabulary (resolves Carry-pending #1)
const STATUS_VALUES = ['PREP', 'VALIDÉ', 'LAUNCHED', 'LIVE', 'CLOSED'];

// Fiscal-year derive helper (mirrors SCHEMA §4.4 fiscal_year derive rule)
function deriveFiscalYear(event) {
  if (event.start_date) return new Date(event.start_date).getUTCFullYear();
  if (event.created_at) return new Date(event.created_at).getUTCFullYear();
  return new Date().getUTCFullYear();
}

/* ----------------------------------------------------------------------------
 * In-memory STATE — mock backend data
 * ------------------------------------------------------------------------- */

const STATE = {
  // SCHEMA §4.1
  qonto_accounts: [
    {
      id: 'q0000001-0000-4000-8000-000000000001',
      name: 'Générale Madame Loyal',
      iban: 'FR76 3000 1000 0100 0000 0000 001',
      is_general: true,
      deleted_at: null,
      created_at: '2024-01-01T00:00:00Z',
      updated_at: '2024-01-01T00:00:00Z',
    },
    {
      id: 'q0000002-0000-4000-8000-000000000002',
      name: 'Paris XXL — compte dédié',
      iban: 'FR76 3000 1000 0100 0000 0000 002',
      is_general: false,
      deleted_at: null,
      created_at: '2025-09-12T10:30:00Z',
      updated_at: '2025-09-12T10:30:00Z',
    },
    {
      id: 'q0000003-0000-4000-8000-000000000003',
      name: 'Bordeaux EPK 2026',
      iban: 'FR76 3000 1000 0100 0000 0000 003',
      is_general: false,
      deleted_at: null,
      created_at: '2026-01-15T14:20:00Z',
      updated_at: '2026-01-15T14:20:00Z',
    },
    {
      id: 'q0000004-0000-4000-8000-000000000004',
      name: 'Festival Avignon 2026',
      iban: 'FR76 3000 1000 0100 0000 0000 004',
      is_general: false,
      deleted_at: null,
      created_at: '2026-02-20T11:00:00Z',
      updated_at: '2026-02-20T11:00:00Z',
    },
  ],

  // SCHEMA §4.2 — V1 captures name only
  coproducers: [
    {
      id: 'c0000001-0000-4000-8000-000000000001',
      name: 'Sound Productions SARL',
      deleted_at: null,
      created_at: '2025-11-04T09:15:00Z',
      updated_at: '2025-11-04T09:15:00Z',
    },
    {
      id: 'c0000002-0000-4000-8000-000000000002',
      name: 'Live Nation France',
      deleted_at: null,
      created_at: '2026-01-20T16:45:00Z',
      updated_at: '2026-01-20T16:45:00Z',
    },
  ],

  // SCHEMA §4.3
  event_series: [
    {
      id: 's0000001-0000-4000-8000-000000000001',
      name: 'Paris XXL',
      city: 'Paris',
      deleted_at: null,
      created_at: '2023-06-01T00:00:00Z',
      updated_at: '2023-06-01T00:00:00Z',
    },
    {
      id: 's0000002-0000-4000-8000-000000000002',
      name: 'Marseille Beach',
      city: 'Marseille',
      deleted_at: null,
      created_at: '2024-05-12T00:00:00Z',
      updated_at: '2024-05-12T00:00:00Z',
    },
  ],

  // SCHEMA §4.4 — 26 columns
  // Sample shape: 1 Générale + 4 active in mixed lifecycle states + 2 sandbox + 2 past
  events: [
    {
      // Générale 2026 — is_general=TRUE annual rollup row
      id: 'e0000000-0000-4000-8000-000000000001',
      name: 'Générale 2026',
      status: 'LIVE',
      is_sandbox: false,
      is_general: true,
      fiscal_year: 2026,
      qonto_account_id: 'q0000001-0000-4000-8000-000000000001',
      series_id: null,
      edition_number: null,
      coproducer_id: null,
      external_coproduction: false,
      producteur: 'EPISODE',
      country: 'France',
      city: null,
      venue: null,
      address: null,
      start_date: '2026-01-01',
      number_of_days: 365,
      budget_target: 0,
      dice_backend_url: null,
      dice_public_url: null,
      shotgun_backend_url: null,
      shotgun_public_url: null,
      deleted_at: null,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    {
      // PREP — future-future, series recurring (Paris XXL 2027 = edition 4)
      id: 'e0000001-0000-4000-8000-000000000001',
      name: 'Paris XXL 2027',
      status: 'PREP',
      is_sandbox: false,
      is_general: false,
      fiscal_year: 2027,
      qonto_account_id: 'q0000002-0000-4000-8000-000000000002',
      series_id: 's0000001-0000-4000-8000-000000000001',
      edition_number: 4,
      coproducer_id: null,
      external_coproduction: false,
      producteur: 'EPISODE',
      country: 'France',
      city: 'Paris',
      venue: 'Parc des Expositions',
      address: '2 Place de la Porte de Versailles, 75015 Paris',
      start_date: '2027-06-04',
      number_of_days: 3,
      budget_target: 1450000,
      dice_backend_url: null,
      dice_public_url: null,
      shotgun_backend_url: null,
      shotgun_public_url: null,
      deleted_at: null,
      created_at: '2026-04-12T11:20:00Z',
      updated_at: '2026-05-02T15:40:00Z',
    },
    {
      // VALIDÉ — confirmed, future, coproducer populated
      id: 'e0000002-0000-4000-8000-000000000002',
      name: 'Bordeaux EPK 2026',
      status: 'VALIDÉ',
      is_sandbox: false,
      is_general: false,
      fiscal_year: 2026,
      qonto_account_id: 'q0000003-0000-4000-8000-000000000003',
      series_id: null,
      edition_number: null,
      coproducer_id: 'c0000001-0000-4000-8000-000000000001',
      external_coproduction: true,
      producteur: 'EPISODE',
      country: 'France',
      city: 'Bordeaux',
      venue: 'Arkéa Arena',
      address: 'Boulevard de Aliénor d\'Aquitaine, 33300 Bordeaux',
      start_date: '2026-09-18',
      number_of_days: 2,
      budget_target: 780000,
      dice_backend_url: 'https://backstage.dice.fm/event/bdx-epk-2026',
      dice_public_url: 'https://dice.fm/event/bdx-epk-2026',
      shotgun_backend_url: null,
      shotgun_public_url: null,
      deleted_at: null,
      created_at: '2025-12-08T10:00:00Z',
      updated_at: '2026-03-15T14:25:00Z',
    },
    {
      // LAUNCHED — tickets on sale, external_coproduction=TRUE without coproducer_id
      id: 'e0000003-0000-4000-8000-000000000003',
      name: 'Marseille BDX 2026',
      status: 'LAUNCHED',
      is_sandbox: false,
      is_general: false,
      fiscal_year: 2026,
      qonto_account_id: 'q0000002-0000-4000-8000-000000000002',
      series_id: 's0000002-0000-4000-8000-000000000002',
      edition_number: 3,
      coproducer_id: null,
      external_coproduction: true,
      producteur: 'EPISODE',
      country: 'France',
      city: 'Marseille',
      venue: 'Plage du Prado',
      address: 'Avenue Pierre Mendès France, 13008 Marseille',
      start_date: '2026-07-11',
      number_of_days: 2,
      budget_target: 620000,
      dice_backend_url: null,
      dice_public_url: null,
      shotgun_backend_url: 'https://promoter.shotgun.live/marseille-bdx-2026',
      shotgun_public_url: 'https://shotgun.live/marseille-bdx-2026',
      deleted_at: null,
      created_at: '2026-01-09T08:30:00Z',
      updated_at: '2026-05-10T17:00:00Z',
    },
    {
      // LIVE — happening this week
      id: 'e0000004-0000-4000-8000-000000000004',
      name: 'Festival Avignon 2026',
      status: 'LIVE',
      is_sandbox: false,
      is_general: false,
      fiscal_year: 2026,
      qonto_account_id: 'q0000004-0000-4000-8000-000000000004',
      series_id: null,
      edition_number: null,
      coproducer_id: 'c0000002-0000-4000-8000-000000000002',
      external_coproduction: true,
      producteur: 'EPISODE',
      country: 'France',
      city: 'Avignon',
      venue: 'Palais des Papes',
      address: 'Place du Palais, 84000 Avignon',
      start_date: '2026-05-14',
      number_of_days: 4,
      budget_target: 950000,
      dice_backend_url: 'https://backstage.dice.fm/event/avignon-2026',
      dice_public_url: 'https://dice.fm/event/avignon-2026',
      shotgun_backend_url: null,
      shotgun_public_url: null,
      deleted_at: null,
      created_at: '2026-02-21T09:00:00Z',
      updated_at: '2026-05-14T18:30:00Z',
    },
    {
      // SANDBOX 1 — minimal scratch event, no dates / no Qonto
      id: 'e0000005-0000-4000-8000-000000000005',
      name: 'Test calcul tournée hiver',
      status: 'PREP',
      is_sandbox: true,
      is_general: false,
      fiscal_year: 2026,                  // fallback to created_at year per SCHEMA §4.4
      qonto_account_id: null,
      series_id: null,
      edition_number: null,
      coproducer_id: null,
      external_coproduction: false,
      producteur: null,
      country: null,
      city: null,
      venue: null,
      address: null,
      start_date: null,
      number_of_days: null,
      budget_target: 250000,
      dice_backend_url: null,
      dice_public_url: null,
      shotgun_backend_url: null,
      shotgun_public_url: null,
      deleted_at: null,
      created_at: '2026-04-30T16:00:00Z',
      updated_at: '2026-04-30T16:00:00Z',
    },
    {
      // SANDBOX 2 — partial fill, dates set but Qonto absent
      id: 'e0000006-0000-4000-8000-000000000006',
      name: 'Sandbox — concept tournée Italie',
      status: 'PREP',
      is_sandbox: true,
      is_general: false,
      fiscal_year: 2026,
      qonto_account_id: null,
      series_id: null,
      edition_number: null,
      coproducer_id: null,
      external_coproduction: false,
      producteur: 'EPISODE',
      country: 'Italie',
      city: 'Milano',
      venue: null,
      address: null,
      start_date: '2026-11-20',
      number_of_days: 2,
      budget_target: 480000,
      dice_backend_url: null,
      dice_public_url: null,
      shotgun_backend_url: null,
      shotgun_public_url: null,
      deleted_at: null,
      created_at: '2026-05-08T11:15:00Z',
      updated_at: '2026-05-08T11:15:00Z',
    },
    {
      // PAST 1 — Paris XXL 2026 (edition 3 of series Paris XXL)
      id: 'e0000007-0000-4000-8000-000000000007',
      name: 'Paris XXL 2026',
      status: 'CLOSED',
      is_sandbox: false,
      is_general: false,
      fiscal_year: 2026,
      qonto_account_id: 'q0000002-0000-4000-8000-000000000002',
      series_id: 's0000001-0000-4000-8000-000000000001',
      edition_number: 3,
      coproducer_id: null,
      external_coproduction: false,
      producteur: 'EPISODE',
      country: 'France',
      city: 'Paris',
      venue: 'Parc des Expositions',
      address: '2 Place de la Porte de Versailles, 75015 Paris',
      start_date: '2026-03-13',
      number_of_days: 3,
      budget_target: 1380000,
      dice_backend_url: 'https://backstage.dice.fm/event/paris-xxl-2026',
      dice_public_url: 'https://dice.fm/event/paris-xxl-2026',
      shotgun_backend_url: null,
      shotgun_public_url: null,
      deleted_at: null,
      created_at: '2025-08-15T13:00:00Z',
      updated_at: '2026-03-20T10:00:00Z',
    },
    {
      // PAST 2 — Lyon one-off 2025, no series
      id: 'e0000008-0000-4000-8000-000000000008',
      name: 'Lyon Nuits Sonores 2025',
      status: 'CLOSED',
      is_sandbox: false,
      is_general: false,
      fiscal_year: 2025,
      qonto_account_id: 'q0000001-0000-4000-8000-000000000001',
      series_id: null,
      edition_number: null,
      coproducer_id: 'c0000001-0000-4000-8000-000000000001',
      external_coproduction: true,
      producteur: 'EPISODE',
      country: 'France',
      city: 'Lyon',
      venue: 'Sucrière',
      address: '49 Quai Rambaud, 69002 Lyon',
      start_date: '2025-05-29',
      number_of_days: 2,
      budget_target: 540000,
      dice_backend_url: null,
      dice_public_url: null,
      shotgun_backend_url: 'https://promoter.shotgun.live/lyon-ns-2025',
      shotgun_public_url: 'https://shotgun.live/lyon-ns-2025',
      deleted_at: null,
      created_at: '2024-11-02T09:00:00Z',
      updated_at: '2025-06-05T15:00:00Z',
    },
  ],

  // SCHEMA §4.5 — child rows for events with start_date populated
  // day_number 1-indexed; capacity NULL for sandbox/pre-confirmation
  event_days: [
    // Générale 2026 — annual rollup, no per-day rows (treated as singleton container)
    // Paris XXL 2027 — 3 days, capacity placeholder (pre-confirmation)
    { id: 'd0001001-0000-4000-8000-000000000001', event_id: 'e0000001-0000-4000-8000-000000000001', day_number: 1, capacity: null, deleted_at: null, created_at: '2026-04-12T11:20:00Z', updated_at: '2026-04-12T11:20:00Z' },
    { id: 'd0001002-0000-4000-8000-000000000002', event_id: 'e0000001-0000-4000-8000-000000000001', day_number: 2, capacity: null, deleted_at: null, created_at: '2026-04-12T11:20:00Z', updated_at: '2026-04-12T11:20:00Z' },
    { id: 'd0001003-0000-4000-8000-000000000003', event_id: 'e0000001-0000-4000-8000-000000000001', day_number: 3, capacity: null, deleted_at: null, created_at: '2026-04-12T11:20:00Z', updated_at: '2026-04-12T11:20:00Z' },
    // Bordeaux EPK 2026 — 2 days, capacity confirmed
    { id: 'd0002001-0000-4000-8000-000000000001', event_id: 'e0000002-0000-4000-8000-000000000002', day_number: 1, capacity: 11000, deleted_at: null, created_at: '2025-12-08T10:00:00Z', updated_at: '2025-12-08T10:00:00Z' },
    { id: 'd0002002-0000-4000-8000-000000000002', event_id: 'e0000002-0000-4000-8000-000000000002', day_number: 2, capacity: 11000, deleted_at: null, created_at: '2025-12-08T10:00:00Z', updated_at: '2025-12-08T10:00:00Z' },
    // Marseille BDX 2026 — 2 days, capacity confirmed
    { id: 'd0003001-0000-4000-8000-000000000001', event_id: 'e0000003-0000-4000-8000-000000000003', day_number: 1, capacity: 8500, deleted_at: null, created_at: '2026-01-09T08:30:00Z', updated_at: '2026-01-09T08:30:00Z' },
    { id: 'd0003002-0000-4000-8000-000000000002', event_id: 'e0000003-0000-4000-8000-000000000003', day_number: 2, capacity: 8500, deleted_at: null, created_at: '2026-01-09T08:30:00Z', updated_at: '2026-01-09T08:30:00Z' },
    // Festival Avignon 2026 — 4 days, capacity confirmed
    { id: 'd0004001-0000-4000-8000-000000000001', event_id: 'e0000004-0000-4000-8000-000000000004', day_number: 1, capacity: 6200, deleted_at: null, created_at: '2026-02-21T09:00:00Z', updated_at: '2026-02-21T09:00:00Z' },
    { id: 'd0004002-0000-4000-8000-000000000002', event_id: 'e0000004-0000-4000-8000-000000000004', day_number: 2, capacity: 6200, deleted_at: null, created_at: '2026-02-21T09:00:00Z', updated_at: '2026-02-21T09:00:00Z' },
    { id: 'd0004003-0000-4000-8000-000000000003', event_id: 'e0000004-0000-4000-8000-000000000004', day_number: 3, capacity: 6200, deleted_at: null, created_at: '2026-02-21T09:00:00Z', updated_at: '2026-02-21T09:00:00Z' },
    { id: 'd0004004-0000-4000-8000-000000000004', event_id: 'e0000004-0000-4000-8000-000000000004', day_number: 4, capacity: 6200, deleted_at: null, created_at: '2026-02-21T09:00:00Z', updated_at: '2026-02-21T09:00:00Z' },
    // Sandbox concept Milano — 2 days, no capacity
    { id: 'd0006001-0000-4000-8000-000000000001', event_id: 'e0000006-0000-4000-8000-000000000006', day_number: 1, capacity: null, deleted_at: null, created_at: '2026-05-08T11:15:00Z', updated_at: '2026-05-08T11:15:00Z' },
    { id: 'd0006002-0000-4000-8000-000000000002', event_id: 'e0000006-0000-4000-8000-000000000006', day_number: 2, capacity: null, deleted_at: null, created_at: '2026-05-08T11:15:00Z', updated_at: '2026-05-08T11:15:00Z' },
    // Paris XXL 2026 (past) — 3 days, capacity confirmed
    { id: 'd0007001-0000-4000-8000-000000000001', event_id: 'e0000007-0000-4000-8000-000000000007', day_number: 1, capacity: 10500, deleted_at: null, created_at: '2025-08-15T13:00:00Z', updated_at: '2025-08-15T13:00:00Z' },
    { id: 'd0007002-0000-4000-8000-000000000002', event_id: 'e0000007-0000-4000-8000-000000000007', day_number: 2, capacity: 10500, deleted_at: null, created_at: '2025-08-15T13:00:00Z', updated_at: '2025-08-15T13:00:00Z' },
    { id: 'd0007003-0000-4000-8000-000000000003', event_id: 'e0000007-0000-4000-8000-000000000007', day_number: 3, capacity: 10500, deleted_at: null, created_at: '2025-08-15T13:00:00Z', updated_at: '2025-08-15T13:00:00Z' },
    // Lyon Nuits Sonores 2025 (past) — 2 days, capacity confirmed
    { id: 'd0008001-0000-4000-8000-000000000001', event_id: 'e0000008-0000-4000-8000-000000000008', day_number: 1, capacity: 4800, deleted_at: null, created_at: '2024-11-02T09:00:00Z', updated_at: '2024-11-02T09:00:00Z' },
    { id: 'd0008002-0000-4000-8000-000000000002', event_id: 'e0000008-0000-4000-8000-000000000008', day_number: 2, capacity: 4800, deleted_at: null, created_at: '2024-11-02T09:00:00Z', updated_at: '2024-11-02T09:00:00Z' },
  ],
};

/* ----------------------------------------------------------------------------
 * Mock function set — one per anticipated endpoint
 * Each returns a Promise. Backend swap: replace body with fetch().
 * ------------------------------------------------------------------------- */

function mockDelay() {
  // Tiny latency to mirror real-world Promise resolution
  return new Promise(resolve => setTimeout(resolve, 50));
}

function clone(obj) {
  // Defensive clone — mock returns copies so callers can't mutate STATE accidentally
  return JSON.parse(JSON.stringify(obj));
}

function getEvents() {
  return mockDelay().then(() =>
    clone(STATE.events.filter(e => !e.deleted_at))
  );
}

function getEvent(id) {
  return mockDelay().then(() => {
    const e = STATE.events.find(ev => ev.id === id && !ev.deleted_at);
    return e ? clone(e) : null;
  });
}

function getEventDays(eventId) {
  return mockDelay().then(() =>
    clone(STATE.event_days.filter(d => d.event_id === eventId && !d.deleted_at))
  );
}

function createEvent(eventInput) {
  return mockDelay().then(() => {
    const id = crypto.randomUUID();
    const now = new Date().toISOString();
    const evt = {
      // Defaults
      id,
      status: 'PREP',
      is_sandbox: false,
      is_general: false,
      qonto_account_id: null,
      series_id: null,
      edition_number: null,
      coproducer_id: null,
      external_coproduction: false,
      producteur: null,
      country: null,
      city: null,
      venue: null,
      address: null,
      start_date: null,
      number_of_days: null,
      budget_target: 0,
      dice_backend_url: null,
      dice_public_url: null,
      shotgun_backend_url: null,
      shotgun_public_url: null,
      deleted_at: null,
      created_at: now,
      updated_at: now,
      // User-provided fields override defaults
      ...eventInput,
    };
    evt.fiscal_year = deriveFiscalYear(evt);
    STATE.events.push(evt);
    return clone(evt);
  });
}

function updateEvent(id, patch) {
  return mockDelay().then(() => {
    const idx = STATE.events.findIndex(e => e.id === id && !e.deleted_at);
    if (idx === -1) return null;
    const now = new Date().toISOString();
    STATE.events[idx] = {
      ...STATE.events[idx],
      ...patch,
      id,                       // id never patches
      updated_at: now,
    };
    STATE.events[idx].fiscal_year = deriveFiscalYear(STATE.events[idx]);
    return clone(STATE.events[idx]);
  });
}

function deleteEvent(id) {
  return mockDelay().then(() => {
    const idx = STATE.events.findIndex(e => e.id === id && !e.deleted_at);
    if (idx === -1) return null;
    const now = new Date().toISOString();
    STATE.events[idx].deleted_at = now;       // SCHEMA §2.2 soft-delete
    STATE.events[idx].updated_at = now;
    return clone(STATE.events[idx]);
  });
}

function getCoproducers() {
  return mockDelay().then(() =>
    clone(STATE.coproducers.filter(c => !c.deleted_at))
  );
}

function createCoproducer(name) {
  return mockDelay().then(() => {
    const now = new Date().toISOString();
    const c = {
      id: crypto.randomUUID(),
      name,
      deleted_at: null,
      created_at: now,
      updated_at: now,
    };
    STATE.coproducers.push(c);
    return clone(c);
  });
}

function getQontoAccounts() {
  return mockDelay().then(() =>
    clone(STATE.qonto_accounts.filter(q => !q.deleted_at))
  );
}

function createQontoAccount(name, iban) {
  return mockDelay().then(() => {
    const now = new Date().toISOString();
    const q = {
      id: crypto.randomUUID(),
      name,
      iban,
      is_general: false,            // new accounts are never the singleton Générale
      deleted_at: null,
      created_at: now,
      updated_at: now,
    };
    STATE.qonto_accounts.push(q);
    return clone(q);
  });
}

function getEventSeries() {
  return mockDelay().then(() =>
    clone(STATE.event_series.filter(s => !s.deleted_at))
  );
}

function createEventSeries(name, city) {
  return mockDelay().then(() => {
    const now = new Date().toISOString();
    const s = {
      id: crypto.randomUUID(),
      name,
      city: city || null,
      deleted_at: null,
      created_at: now,
      updated_at: now,
    };
    STATE.event_series.push(s);
    return clone(s);
  });
}

// Save-as-template stub — V1 mock returns a fake template id.
// Per brief §4 architectural note: templates are BudgetFlow-owned. Module Dates
// calls BudgetFlow API. V1 mock returns Promise.resolve with a fake id.
function saveAsTemplate(eventId, templateName) {
  return mockDelay().then(() => ({
    id: crypto.randomUUID(),                // would come from BudgetFlow API
    name: templateName,
    source_event_id: eventId,
    created_at: new Date().toISOString(),
  }));
}

/* ----------------------------------------------------------------------------
 * Public surface — globals exposed for app.js consumption
 * (no module loader V1; vanilla script tags per ADR-0007 §5 GitHub Pages V1)
 * ------------------------------------------------------------------------- */

window.MD = {
  CURRENT_USER,
  STATUS_VALUES,
  // Reads
  getEvents,
  getEvent,
  getEventDays,
  getCoproducers,
  getQontoAccounts,
  getEventSeries,
  // Writes
  createEvent,
  updateEvent,
  deleteEvent,
  createCoproducer,
  createQontoAccount,
  createEventSeries,
  saveAsTemplate,
};
