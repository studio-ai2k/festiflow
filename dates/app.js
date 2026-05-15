/* ============================================================================
 * Module Dates — app.js
 * ----------------------------------------------------------------------------
 * Vanilla JS. No framework. Consumes window.MD.* exposed by mock-data.js.
 * NEVER calls real fetch() — all data flows through MD.* mock functions
 * returning Promises (per scope lock #1: frontend-only V1).
 *
 * Architecture:
 *   - Hash-based router (no history API V1 per ADR-0009 single-repo path).
 *   - Persistent chrome (pnav + mnav + sw session switcher) lives in
 *     index.html; pages are .page sections toggled by router.
 *   - Event create/edit = modal within index.html (not separate page).
 *
 * Carry-pending markers honored:
 *   #1 status enum 5-value (PREP/VALIDÉ/LAUNCHED/LIVE/CLOSED)
 *   #2 is_sandbox BOOLEAN (drops validation when ON)
 *   #4 singular coproducer V1 + external_coproduction BOOLEAN
 *   #6 fiche read contract = flat row + separate cached dropdown calls
 * ========================================================================= */

'use strict';

/* ----------------------------------------------------------------------------
 * Module-scoped state — read-only views over mock data, never the source.
 * Dropdowns cached at boot per SCHEMA #6 fiche read contract.
 * ------------------------------------------------------------------------- */

const APP_STATE = {
  // Cached lookups — populated at boot via parallel MD.* reads
  coproducers: [],
  qonto_accounts: [],
  event_series: [],

  // List view UI state
  filter: 'upcoming',    // 'upcoming' | 'live' | 'past'
  showSandbox: false,
  currentSession: null,  // event id currently selected in session switcher

  // Modal state
  modalMode: null,       // 'create' | 'edit' | null
  modalEventId: null,    // populated when editing
  modalDirty: false,     // unsaved-changes guard
};

/* ============================================================================
 * Helpers
 * ========================================================================= */

function $(sel, root) {
  return (root || document).querySelector(sel);
}

function $$(sel, root) {
  return Array.from((root || document).querySelectorAll(sel));
}

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') node.className = v;
      else if (k === 'dataset') Object.assign(node.dataset, v);
      else if (k === 'on') {
        for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
      }
      else if (k === 'html') node.innerHTML = v;
      else if (v == null || v === false) continue;
      else node.setAttribute(k, v === true ? '' : String(v));
    }
  }
  if (children != null) {
    const arr = Array.isArray(children) ? children : [children];
    for (const c of arr) {
      if (c == null || c === false) continue;
      node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
  }
  return node;
}

function fmtDateFR(iso) {
  if (!iso) return '—';
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short', year: 'numeric' });
}

function fmtMonthFR(iso) {
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString('fr-FR', { month: 'long', year: 'numeric' });
}

function addDaysISO(iso, n) {
  const d = new Date(iso + 'T00:00:00');
  d.setDate(d.getDate() + n);
  return d.toISOString().slice(0, 10);
}

function daysUntil(iso) {
  if (!iso) return null;
  const now = new Date();
  now.setHours(0, 0, 0, 0);
  const target = new Date(iso + 'T00:00:00');
  return Math.round((target - now) / 86400000);
}

function todayISO() {
  return new Date().toISOString().slice(0, 10);
}

function lookupName(collection, id) {
  if (!id) return '—';
  const found = collection.find((x) => x.id === id);
  return found ? found.name : '—';
}

/* ============================================================================
 * Filter logic
 * ----------------------------------------------------------------------------
 * "À venir" = future start_date AND status in {PREP, VALIDÉ, LAUNCHED}
 * "En cours" = status === 'LIVE' (regardless of start_date)
 * "Passés"   = status === 'CLOSED' OR start_date strictly before today
 *              (also accessible via Anciens view — same predicate)
 * Sandbox events are filtered out unless APP_STATE.showSandbox is true.
 * Générale (is_general=true) is always shown in Liste regardless of filter,
 * pinned at top — it's the annual rollup row, not a filterable event.
 * ========================================================================= */

function filterEvents(events, filter, opts) {
  const showSandbox = opts && opts.showSandbox;
  const today = todayISO();

  return events.filter((e) => {
    if (e.is_sandbox && !showSandbox) return false;
    // Générale shown only in Liste with upcoming/live filters; not in Passés
    if (e.is_general) return filter !== 'past';

    if (filter === 'upcoming') {
      return (
        e.start_date >= today &&
        ['PREP', 'VALIDÉ', 'LAUNCHED'].includes(e.status)
      );
    }
    if (filter === 'live') {
      return e.status === 'LIVE';
    }
    if (filter === 'past') {
      return e.status === 'CLOSED' || (e.start_date && e.start_date < today);
    }
    return true;
  });
}

/* ============================================================================
 * Card renderer — used by Liste + Anciens
 * ========================================================================= */

function renderEventCard(event) {
  const classes = ['ecard'];
  if (event.is_sandbox) classes.push('sandbox');
  if (event.is_general) classes.push('general');

  const editHref = '#/event/' + event.id + '/edit';

  // Status chip — CSS class mirrors enum value lowercased (incl. accents).
  // Vars: .chip-prep / .chip-validé / .chip-launched / .chip-live / .chip-closed
  const statusChipClass = 'chip chip-' + event.status.toLowerCase();
  const statusChip = el('span', { class: statusChipClass }, event.status);

  // Modifier chips (sandbox / general / external coprod)
  const modifierChips = [];
  if (event.is_sandbox) modifierChips.push(el('span', { class: 'chip chip-sandbox' }, 'SANDBOX'));
  if (event.is_general) modifierChips.push(el('span', { class: 'chip chip-general' }, 'GÉNÉRALE'));
  if (event.external_coproduction) modifierChips.push(el('span', { class: 'chip chip-extcoprod' }, 'COPROD EXT.'));

  // Header
  const hdr = el('div', { class: 'ecard-hdr' }, [
    el('h3', { class: 'ecard-name' }, event.name),
    el('div', { class: 'ecard-chips' }, [statusChip, ...modifierChips]),
  ]);

  // Meta — city / date / capacity / qonto account
  const metaRows = [];
  if (event.city || event.country) {
    metaRows.push(el('div', { class: 'ecard-meta-row' }, [
      el('span', { class: 'ecard-meta-label' }, 'Lieu'),
      el('span', { class: 'ecard-meta-val' }, [event.city, event.country].filter(Boolean).join(', ')),
    ]));
  }
  if (event.start_date && !event.is_general) {
    const dur = event.number_of_days > 1
      ? fmtDateFR(event.start_date) + ' → ' + fmtDateFR(addDaysISO(event.start_date, event.number_of_days - 1))
      : fmtDateFR(event.start_date);
    metaRows.push(el('div', { class: 'ecard-meta-row' }, [
      el('span', { class: 'ecard-meta-label' }, 'Dates'),
      el('span', { class: 'ecard-meta-val' }, dur),
    ]));

    // Countdown hint for upcoming
    const dt = daysUntil(event.start_date);
    if (dt !== null && dt > 0 && dt <= 90) {
      metaRows.push(el('div', { class: 'ecard-meta-row' }, [
        el('span', { class: 'ecard-meta-label' }, 'Dans'),
        el('span', { class: 'ecard-meta-val' }, dt + ' jour' + (dt > 1 ? 's' : '')),
      ]));
    }
  }
  if (event.qonto_account_id) {
    metaRows.push(el('div', { class: 'ecard-meta-row' }, [
      el('span', { class: 'ecard-meta-label' }, 'Qonto'),
      el('span', { class: 'ecard-meta-val' }, lookupName(APP_STATE.qonto_accounts, event.qonto_account_id)),
    ]));
  }
  if (event.coproducer_id) {
    metaRows.push(el('div', { class: 'ecard-meta-row' }, [
      el('span', { class: 'ecard-meta-label' }, 'Coprod.'),
      el('span', { class: 'ecard-meta-val' }, lookupName(APP_STATE.coproducers, event.coproducer_id)),
    ]));
  }

  const meta = el('div', { class: 'ecard-meta' }, metaRows);

  // Footer with edit link
  const foot = el('div', { class: 'ecard-foot' }, [
    el('a', { class: 'ecard-edit-link', href: editHref }, 'Ouvrir →'),
  ]);

  return el('article', { class: classes.join(' '), dataset: { eventId: event.id } }, [
    hdr, meta, foot,
  ]);
}

/* ============================================================================
 * Page renderers
 * ========================================================================= */

function renderListe() {
  const grid = $('#egrid-liste');
  const empty = $('#empty-liste');
  grid.innerHTML = '';

  window.MD.getEvents().then((events) => {
    const filtered = filterEvents(events, APP_STATE.filter, { showSandbox: APP_STATE.showSandbox });

    if (filtered.length === 0) {
      empty.hidden = false;
      grid.hidden = true;
      return;
    }
    empty.hidden = true;
    grid.hidden = false;

    // Pin Générale first if present
    filtered.sort((a, b) => {
      if (a.is_general && !b.is_general) return -1;
      if (!a.is_general && b.is_general) return 1;
      if (!a.start_date) return 1;
      if (!b.start_date) return -1;
      return a.start_date.localeCompare(b.start_date);
    });

    for (const event of filtered) {
      grid.appendChild(renderEventCard(event));
    }
  });
}

function renderCalendrier() {
  const root = $('#ecal');
  const empty = $('#empty-cal');
  root.innerHTML = '';

  window.MD.getEvents().then((events) => {
    // Calendar shows upcoming + live, ignoring sandbox toggle
    const today = todayISO();
    const visible = events.filter((e) => {
      if (e.is_sandbox) return false;
      if (e.is_general) return false;
      return e.status !== 'CLOSED' && (!e.start_date || e.start_date >= today);
    });

    if (visible.length === 0) {
      empty.hidden = false;
      root.hidden = true;
      return;
    }
    empty.hidden = true;
    root.hidden = false;

    visible.sort((a, b) => (a.start_date || '').localeCompare(b.start_date || ''));

    // Group by YYYY-MM
    const groups = {};
    for (const e of visible) {
      const key = (e.start_date || 'undated').slice(0, 7);
      if (!groups[key]) groups[key] = [];
      groups[key].push(e);
    }

    for (const monthKey of Object.keys(groups).sort()) {
      const monthLabel = monthKey === 'undated'
        ? 'Sans date'
        : fmtMonthFR(monthKey + '-01');

      const monthBlock = el('section', { class: 'ecal-month' }, [
        el('h2', { class: 'ecal-month-title' }, monthLabel),
        el('div', { class: 'ecal-month-rows' },
          groups[monthKey].map((e) => {
            const statusChipClass = 'chip chip-' + e.status.toLowerCase();
            return el('a', {
              class: 'ecal-row',
              href: '#/event/' + e.id + '/edit',
              dataset: { eventId: e.id },
            }, [
              el('div', { class: 'ecal-row-date' }, fmtDateFR(e.start_date)),
              el('div', { class: 'ecal-row-name' }, e.name),
              el('div', { class: 'ecal-row-meta' }, [
                e.city ? el('span', { class: 'ecal-row-city' }, e.city) : null,
                el('span', { class: statusChipClass }, e.status),
              ]),
            ]);
          })),
      ]);
      root.appendChild(monthBlock);
    }
  });
}

function renderTemplates() {
  const root = $('#tpl-list');
  const empty = $('#empty-tpl');
  root.innerHTML = '';

  // V1: no template state in mock-data (saveAsTemplate returns fake ids only).
  // Show empty state with hint pointing users to event fiche.
  empty.hidden = false;
  root.hidden = true;
}

function renderAnciens() {
  const grid = $('#egrid-anciens');
  const empty = $('#empty-anciens');
  grid.innerHTML = '';

  window.MD.getEvents().then((events) => {
    const today = todayISO();
    const past = events.filter((e) => {
      if (e.is_sandbox && !APP_STATE.showSandbox) return false;
      if (e.is_general) return false;
      return e.status === 'CLOSED' || (e.start_date && e.start_date < today);
    });

    if (past.length === 0) {
      empty.hidden = false;
      grid.hidden = true;
      return;
    }
    empty.hidden = true;
    grid.hidden = false;

    // Sort newest-first
    past.sort((a, b) => (b.start_date || '').localeCompare(a.start_date || ''));

    for (const event of past) {
      grid.appendChild(renderEventCard(event));
    }
  });
}

/* ============================================================================
 * Session switcher (.sw)
 * ----------------------------------------------------------------------------
 * Lists current LIVE / VALIDÉ / LAUNCHED events + Générale (pinned at top).
 * Selection persists via localStorage. URL param `?session=<id>` overrides.
 * ========================================================================= */

const SESSION_KEY = 'md.currentSession';

function renderSessionMenu() {
  const menu = $('#sw-menu');
  menu.innerHTML = '';

  window.MD.getEvents().then((events) => {
    // Active = non-sandbox, non-closed; Générale pinned first.
    const active = events.filter((e) => !e.is_sandbox && e.status !== 'CLOSED');
    active.sort((a, b) => {
      if (a.is_general && !b.is_general) return -1;
      if (!a.is_general && b.is_general) return 1;
      return (a.start_date || '').localeCompare(b.start_date || '');
    });

    // Resolve current session: URL param > localStorage > Générale (if exists) > first active
    const urlSession = new URLSearchParams(location.search).get('session');
    const stored = localStorage.getItem(SESSION_KEY);
    const generale = active.find((e) => e.is_general);
    let current = active.find((e) => e.id === urlSession)
      || active.find((e) => e.id === stored)
      || generale
      || active[0]
      || null;

    APP_STATE.currentSession = current ? current.id : null;
    $('#sw-btn-name').textContent = current ? current.name : '—';

    for (const e of active) {
      const opt = el('button', {
        class: 'sw-opt' + (e.is_general ? ' is-general' : '') + (current && e.id === current.id ? ' active' : ''),
        type: 'button',
        dataset: { eventId: e.id },
      }, [
        el('span', { class: 'sw-opt-name' }, e.name),
        el('span', { class: 'sw-opt-meta' },
          e.is_general
            ? 'Année fiscale ' + e.fiscal_year
            : (e.start_date ? fmtDateFR(e.start_date) : '—')),
      ]);
      opt.addEventListener('click', () => {
        APP_STATE.currentSession = e.id;
        localStorage.setItem(SESSION_KEY, e.id);
        $('#sw-btn-name').textContent = e.name;
        $$('.sw-opt').forEach((o) => o.classList.toggle('active', o.dataset.eventId === e.id));
        closeSessionMenu();
      });
      menu.appendChild(opt);
    }
  });
}

function openSessionMenu() {
  $('#sw-menu').classList.add('open');
  $('#sw-btn').setAttribute('aria-expanded', 'true');
}

function closeSessionMenu() {
  $('#sw-menu').classList.remove('open');
  $('#sw-btn').setAttribute('aria-expanded', 'false');
}

/* ============================================================================
 * Modal — event create / edit
 * ========================================================================= */

function populateModalDropdowns() {
  // Series
  const series = $('#f-series');
  // Clear all but first option
  while (series.options.length > 1) series.remove(1);
  for (const s of APP_STATE.event_series) {
    series.add(new Option(s.name + ' — ' + s.city, s.id));
  }

  // Coproducers
  const coprod = $('#f-coproducer');
  while (coprod.options.length > 1) coprod.remove(1);
  for (const c of APP_STATE.coproducers) {
    coprod.add(new Option(c.name, c.id));
  }

  // Qonto accounts — Générale account flagged in label
  const qonto = $('#f-qonto-account');
  while (qonto.options.length > 1) qonto.remove(1);
  for (const q of APP_STATE.qonto_accounts) {
    const label = q.is_general ? '★ ' + q.name + ' (Générale)' : q.name;
    qonto.add(new Option(label, q.id));
  }
}

function generateCapacityRows(startDateISO, numberOfDays, existing) {
  const root = $('#cap-rows');
  root.innerHTML = '';
  if (!startDateISO || !numberOfDays || numberOfDays < 1) return;

  const max = Math.min(numberOfDays, 365);
  for (let i = 0; i < max; i++) {
    const dayDate = addDaysISO(startDateISO, i);
    const existingDay = existing && existing.find((d) => d.day_index === i + 1);
    const row = el('div', { class: 'cap-row' }, [
      el('div', { class: 'cap-row-label' }, [
        el('span', { class: 'cap-row-num' }, 'J' + (i + 1)),
        el('span', { class: 'cap-row-date' }, fmtDateFR(dayDate)),
      ]),
      el('input', {
        type: 'number',
        class: 'cap-row-input',
        min: '0',
        step: '1',
        placeholder: 'Jauge',
        value: existingDay ? existingDay.capacity : '',
        dataset: { dayIndex: String(i + 1), dayDate },
      }),
    ]);
    root.appendChild(row);
  }
}

function openModalCreate() {
  APP_STATE.modalMode = 'create';
  APP_STATE.modalEventId = null;
  APP_STATE.modalDirty = false;

  $('#emodal-title').textContent = 'Nouvel événement';
  $('#btn-save-as-template').hidden = true;
  $('#event-form').reset();
  $('#cap-rows').innerHTML = '';

  // Pre-set producteur default + status default
  $('#f-producteur').value = 'EPISODE';
  $('#f-status').value = 'PREP';
  $('#f-number-of-days').value = '1';
  $('#f-create-budget').checked = true;
  $('#f-create-billetterie').checked = false;
  $('#f-create-medias').checked = false;

  populateModalDropdowns();
  clearAllErrors();
  syncSandboxVisuals();

  $('#emodal-overlay').hidden = false;
  requestAnimationFrame(() => {
    $('#emodal-overlay').classList.add('open');
    $('#emodal').classList.add('open');
    $('#f-name').focus();
  });
}

function openModalEdit(eventId) {
  APP_STATE.modalMode = 'edit';
  APP_STATE.modalEventId = eventId;
  APP_STATE.modalDirty = false;

  Promise.all([
    window.MD.getEvent(eventId),
    window.MD.getEventDays(eventId),
  ]).then(([event, days]) => {
    if (!event) {
      console.warn('Événement introuvable:', eventId);
      location.hash = '#/liste';
      return;
    }

    $('#emodal-title').textContent = 'Modifier — ' + event.name;
    $('#btn-save-as-template').hidden = event.is_general; // can't template the Générale
    $('#event-form').reset();
    populateModalDropdowns();

    // Hydrate fields
    $('#f-name').value = event.name || '';
    $('#f-sandbox').checked = !!event.is_sandbox;
    $('#f-country').value = event.country || '';
    $('#f-city').value = event.city || '';
    $('#f-venue').value = event.venue || '';
    $('#f-address').value = event.address || '';
    $('#f-start-date').value = event.start_date || '';
    $('#f-number-of-days').value = event.number_of_days || 1;
    $('#f-producteur').value = event.producteur || '';
    $('#f-series').value = event.series_id || '';
    $('#f-edition').value = event.edition_number || '';
    $('#f-coproducer').value = event.coproducer_id || '';
    $('#f-external-coproduction').checked = !!event.external_coproduction;
    $('#f-dice-backend').value = event.dice_backend_url || '';
    $('#f-dice-public').value = event.dice_public_url || '';
    $('#f-shotgun-backend').value = event.shotgun_backend_url || '';
    $('#f-shotgun-public').value = event.shotgun_public_url || '';
    $('#f-status').value = event.status || 'PREP';
    $('#f-budget-target').value = event.budget_target || '';
    $('#f-qonto-account').value = event.qonto_account_id || '';

    // Capacity rows hydrate from event_days
    generateCapacityRows(event.start_date, event.number_of_days, days);

    clearAllErrors();
    syncSandboxVisuals();

    $('#emodal-overlay').hidden = false;
    requestAnimationFrame(() => {
      $('#emodal-overlay').classList.add('open');
      $('#emodal').classList.add('open');
    });
  });
}

function closeModal() {
  if (APP_STATE.modalDirty && !confirm('Modifications non enregistrées. Fermer quand même ?')) {
    return;
  }
  $('#emodal-overlay').classList.remove('open');
  $('#emodal').classList.remove('open');
  setTimeout(() => {
    $('#emodal-overlay').hidden = true;
    APP_STATE.modalMode = null;
    APP_STATE.modalEventId = null;
    APP_STATE.modalDirty = false;
    // Navigate back to current page route
    const r = parseRoute(location.hash);
    if (r.kind === 'event') location.hash = '#/liste';
  }, 180);
}

function syncSandboxVisuals() {
  const isSandbox = $('#f-sandbox').checked;
  $('#f-sandbox-wrap').classList.toggle('on', isSandbox);
  // Visually relax the "required" indicators on sandbox events.
  // Validation logic itself handles the actual relax — this is just affordance.
  $$('.efld-label.req').forEach((lbl) => {
    lbl.classList.toggle('req-dim', isSandbox);
  });
}

/* ----------------------------------------------------------------------------
 * Validation per briefs/evenements.md §11 Geoffrey-locked contract:
 *   ALWAYS required: name
 *   Required when NOT sandbox: country, city, start_date, number_of_days,
 *                              capacity-per-day (at least 1 row with value > 0),
 *                              producteur, qonto_account_id
 *   Optional always: coproducer_id, dice/shotgun URLs, budget_target,
 *                    venue, address, edition_number, series_id
 * ------------------------------------------------------------------------- */

function clearAllErrors() {
  $$('.efld-err').forEach((e) => { e.hidden = true; e.textContent = ''; });
  $$('.efld input.err, .efld select.err').forEach((i) => i.classList.remove('err'));
}

// Maps validation field names to the actual DOM id of the input.
// Most ids derive mechanically (snake → kebab), but qonto_account_id has a
// shorter id (#f-qonto-account, not #f-qonto-account-id) and `capacity` is
// a fieldset-level error with no matching single input.
const FIELD_INPUT_ID = {
  name: 'f-name',
  country: 'f-country',
  city: 'f-city',
  start_date: 'f-start-date',
  number_of_days: 'f-number-of-days',
  producteur: 'f-producteur',
  qonto_account_id: 'f-qonto-account',
  capacity: null,
};

function showError(name, msg) {
  const errNode = $('[data-err-for="' + name + '"]');
  if (errNode) {
    errNode.hidden = false;
    errNode.textContent = msg;
  }
  const inputId = FIELD_INPUT_ID[name];
  if (inputId) {
    const input = $('#' + inputId);
    if (input) input.classList.add('err');
  }
}

function validateForm() {
  clearAllErrors();
  const errors = [];
  const isSandbox = $('#f-sandbox').checked;

  // Always required
  const name = $('#f-name').value.trim();
  if (!name) errors.push(['name', 'Le nom est obligatoire.']);

  if (!isSandbox) {
    const country = $('#f-country').value.trim();
    if (!country) errors.push(['country', 'Le pays est obligatoire.']);

    const city = $('#f-city').value.trim();
    if (!city) errors.push(['city', 'La ville est obligatoire.']);

    const startDate = $('#f-start-date').value;
    if (!startDate) errors.push(['start_date', 'La date de début est obligatoire.']);

    const nDays = parseInt($('#f-number-of-days').value, 10);
    if (!nDays || nDays < 1) errors.push(['number_of_days', 'Le nombre de jours doit être ≥ 1.']);

    const producteur = $('#f-producteur').value;
    if (!producteur) errors.push(['producteur', 'Le producteur est obligatoire.']);

    const qonto = $('#f-qonto-account').value;
    if (!qonto) errors.push(['qonto_account_id', 'Un compte Qonto est obligatoire.']);

    // Capacity: at least one row with value > 0
    const capInputs = $$('.cap-row-input');
    const hasAnyCap = capInputs.some((i) => parseInt(i.value, 10) > 0);
    if (capInputs.length === 0 || !hasAnyCap) {
      errors.push(['capacity', 'Renseigne une jauge pour au moins un jour.']);
    }
  }

  for (const [field, msg] of errors) showError(field, msg);
  return errors.length === 0;
}

function collectFormPayload() {
  const isSandbox = $('#f-sandbox').checked;
  const startDate = $('#f-start-date').value || null;
  const nDays = parseInt($('#f-number-of-days').value, 10) || 1;

  const capacityRows = $$('.cap-row-input').map((input) => ({
    day_index: parseInt(input.dataset.dayIndex, 10),
    day_date: input.dataset.dayDate,
    capacity: parseInt(input.value, 10) || 0,
  }));

  return {
    name: $('#f-name').value.trim(),
    is_sandbox: isSandbox,
    is_general: false,                       // user-created events are never Générale (annual rollup is system-managed)
    status: $('#f-status').value || 'PREP',
    qonto_account_id: $('#f-qonto-account').value || null,
    series_id: $('#f-series').value || null,
    edition_number: parseInt($('#f-edition').value, 10) || null,
    coproducer_id: $('#f-coproducer').value || null,
    external_coproduction: $('#f-external-coproduction').checked,
    producteur: $('#f-producteur').value || null,
    country: $('#f-country').value.trim() || null,
    city: $('#f-city').value.trim() || null,
    venue: $('#f-venue').value.trim() || null,
    address: $('#f-address').value.trim() || null,
    start_date: startDate,
    number_of_days: nDays,
    budget_target: parseFloat($('#f-budget-target').value) || null,
    dice_backend_url: $('#f-dice-backend').value.trim() || null,
    dice_public_url: $('#f-dice-public').value.trim() || null,
    shotgun_backend_url: $('#f-shotgun-backend').value.trim() || null,
    shotgun_public_url: $('#f-shotgun-public').value.trim() || null,
    event_days: capacityRows,
    // Downstream creation toggles — V1 mockup only
    _create_budget_sheet: $('#f-create-budget').checked,
    _create_billetterie_sheet: $('#f-create-billetterie').checked,
    _create_medias_brief: $('#f-create-medias').checked,
  };
}

function handleSave() {
  if (!validateForm()) {
    // Scroll to first error
    const firstErr = $('.efld-err:not([hidden])');
    if (firstErr) firstErr.scrollIntoView({ block: 'center', behavior: 'smooth' });
    return;
  }

  const payload = collectFormPayload();
  const saveBtn = $('#btn-save');
  saveBtn.disabled = true;
  saveBtn.textContent = 'Enregistrement…';

  const op = APP_STATE.modalMode === 'create'
    ? window.MD.createEvent(payload)
    : window.MD.updateEvent(APP_STATE.modalEventId, payload);

  op.then((result) => {
    APP_STATE.modalDirty = false;
    saveBtn.disabled = false;
    saveBtn.textContent = 'Enregistrer';
    closeModal();
    // Re-render current page + session menu (new event might appear there)
    renderSessionMenu();
    renderCurrentPage();
  }).catch((err) => {
    console.error('Save failed:', err);
    saveBtn.disabled = false;
    saveBtn.textContent = 'Enregistrer';
    alert('Erreur lors de l\'enregistrement.');
  });
}

function handleSaveAsTemplate() {
  if (APP_STATE.modalMode !== 'edit' || !APP_STATE.modalEventId) return;
  const tplName = prompt('Nom du template ?', $('#f-name').value + ' — modèle');
  if (!tplName) return;
  window.MD.saveAsTemplate(APP_STATE.modalEventId, tplName).then(() => {
    alert('Template sauvegardé. (V1 mockup — non persistant.)');
  });
}

/* ============================================================================
 * Router
 * ----------------------------------------------------------------------------
 * Routes:
 *   #/liste         (default)
 *   #/calendrier
 *   #/templates
 *   #/anciens
 *   #/event/new
 *   #/event/:id/edit
 * ========================================================================= */

function parseRoute(hash) {
  const h = (hash || '').replace(/^#\/?/, '');
  if (!h || h === '' || h === 'liste') return { kind: 'page', name: 'liste' };
  if (h === 'calendrier') return { kind: 'page', name: 'calendrier' };
  if (h === 'templates') return { kind: 'page', name: 'templates' };
  if (h === 'anciens') return { kind: 'page', name: 'anciens' };
  if (h === 'event/new') return { kind: 'event', op: 'new' };
  const m = h.match(/^event\/([^/]+)\/edit$/);
  if (m) return { kind: 'event', op: 'edit', id: m[1] };
  return { kind: 'page', name: 'liste' };
}

function renderCurrentPage() {
  const r = parseRoute(location.hash);
  if (r.kind !== 'page') return;
  if (r.name === 'liste') renderListe();
  else if (r.name === 'calendrier') renderCalendrier();
  else if (r.name === 'templates') renderTemplates();
  else if (r.name === 'anciens') renderAnciens();
}

function navigate() {
  const r = parseRoute(location.hash);

  if (r.kind === 'event') {
    // Render the underlying page first so closing the modal returns to liste
    if (!$('#page-liste').classList.contains('on')) {
      activatePage('liste');
    }
    if (r.op === 'new') openModalCreate();
    else if (r.op === 'edit') openModalEdit(r.id);
    return;
  }

  // Page navigation
  activatePage(r.name);
  renderCurrentPage();
}

function activatePage(name) {
  $$('.page').forEach((p) => p.classList.toggle('on', p.dataset.route === name));
  $$('.mnav-item').forEach((a) => a.classList.toggle('on', a.dataset.route === name));
}

/* ============================================================================
 * Wire-up
 * ========================================================================= */

function bindFilterChips() {
  $$('#echip-row .echip').forEach((chip) => {
    chip.addEventListener('click', () => {
      const f = chip.dataset.filter;
      if (!f) return;
      APP_STATE.filter = f;
      $$('#echip-row .echip').forEach((c) => c.classList.toggle('on', c === chip));
      renderListe();
    });
  });

  const stog = $('#sandbox-toggle');
  stog.addEventListener('click', () => {
    APP_STATE.showSandbox = !APP_STATE.showSandbox;
    stog.classList.toggle('on', APP_STATE.showSandbox);
    stog.setAttribute('aria-pressed', String(APP_STATE.showSandbox));
    renderListe();
  });
}

function bindSessionSwitcher() {
  $('#sw-btn').addEventListener('click', () => {
    const isOpen = $('#sw-menu').classList.contains('open');
    if (isOpen) closeSessionMenu();
    else openSessionMenu();
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.sw')) closeSessionMenu();
  });
}

function bindModalEvents() {
  $('#emodal-close').addEventListener('click', closeModal);
  $('#btn-cancel').addEventListener('click', closeModal);
  $('#btn-save').addEventListener('click', handleSave);
  $('#btn-save-as-template').addEventListener('click', handleSaveAsTemplate);

  // Close on overlay click (outside modal body)
  $('#emodal-overlay').addEventListener('click', (e) => {
    if (e.target.id === 'emodal-overlay') closeModal();
  });

  // Esc closes
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && APP_STATE.modalMode) closeModal();
  });

  // Sandbox toggle re-renders validation affordance
  $('#f-sandbox').addEventListener('change', () => {
    syncSandboxVisuals();
    APP_STATE.modalDirty = true;
  });

  // Capacity rows regenerate when start_date or number_of_days change
  const regen = () => {
    const sd = $('#f-start-date').value;
    const nd = parseInt($('#f-number-of-days').value, 10) || 1;
    if (sd && nd >= 1) generateCapacityRows(sd, nd);
  };
  $('#f-start-date').addEventListener('change', () => { regen(); APP_STATE.modalDirty = true; });
  $('#f-number-of-days').addEventListener('change', () => { regen(); APP_STATE.modalDirty = true; });

  // Generic dirty tracking
  $('#event-form').addEventListener('input', () => { APP_STATE.modalDirty = true; });
  $('#event-form').addEventListener('change', () => { APP_STATE.modalDirty = true; });
}

function bindNewEventButtons() {
  $('#btn-new-event').addEventListener('click', () => { location.hash = '#/event/new'; });
  $('#btn-new-event-cal').addEventListener('click', () => { location.hash = '#/event/new'; });
}

/* ============================================================================
 * Boot
 * ========================================================================= */

function boot() {
  // Pre-cache dropdown sources in parallel (SCHEMA #6 read-contract pattern)
  Promise.all([
    window.MD.getCoproducers(),
    window.MD.getQontoAccounts(),
    window.MD.getEventSeries(),
  ]).then(([coproducers, qontoAccounts, eventSeries]) => {
    APP_STATE.coproducers = coproducers;
    APP_STATE.qonto_accounts = qontoAccounts;
    APP_STATE.event_series = eventSeries;

    bindFilterChips();
    bindSessionSwitcher();
    bindModalEvents();
    bindNewEventButtons();

    window.addEventListener('hashchange', navigate);

    // Default landing = #/liste if no hash present. We deliberately don't
    // assign location.hash when it's already valid — assigning the same
    // value doesn't fire hashchange, but also doesn't no-op the render
    // path. Simpler: route from whatever is currently in the URL.
    if (!location.hash || location.hash === '#' || location.hash === '#/') {
      // Replace (don't push) so back-button doesn't return to empty hash
      history.replaceState(null, '', location.pathname + location.search + '#/liste');
    }
    navigate();

    renderSessionMenu();
  }).catch((err) => {
    console.error('Boot failed:', err);
    document.body.innerHTML = '<div style="padding:40px;color:#f87171;font-family:sans-serif">' +
      'Erreur de chargement. Recharge la page.</div>';
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
