# billetterie/ — ticketing data fetch (festiflow V1)

Fetches live ticketing data from DICE + Shotgun, emits one JSON object per event (the contract the Module Dates drawer + BudgetFlow recettes consume). Pure-Python, zero pip dependencies. This is the DATA layer — it owns fetching/calculating, not rendering.

**This is NEW. The legacy operational tool (madameloyal repo + Railway) is untouched and stays live until V1-operational.**

## What's in here
- `festiflow_fetch.py` — the fetch + classify + aggregate script (stdlib only).
- `event_config.csv` — per-event platform IDs + days. Bordeaux 2026/2025 seeded (CONFIRM the `dice_mio_id` values — placeholders marked `DICE_MIO_ID_CONFIRM`).
- `.gitignore` — keeps tokens + generated JSON out of git.
- (no `requirements.txt` needed — the script uses only Python standard library.)

## CONFIRM before first run (config gaps left for you/the billetterie chat)
- `dice_mio_id` for Bordeaux 2026 and 2025 — get from the DICE partner backend (the door-list URLs / MIO dashboard). Shotgun IDs are filled (2026 = 505434, 2025 = 408231).
- `day_capacity` per day — currently 0 (fill venue capacities; affects fill-rate KPIs only, not revenue).
- Whether 2025 comparison is LIVE (both are our own events → fetchable) or stored — seeded as live `compare_to: bordeaux_2025`.

## How to run it — you're on iPad, so NO local terminal. Two real paths:

### A. PROVE IT FIRST — GitHub Codespaces (browser terminal, from iPad Safari)
Codespaces gives you a real terminal in the browser — no Mac needed.
1. In the festiflow repo on github.com (Safari) → Code ▸ Codespaces ▸ create.
2. In the Codespace terminal:
   ```
   cd billetterie
   SHOTGUN_TOKEN=xxx DICE_TOKEN=xxx python festiflow_fetch.py bordeaux_2026 --pretty
   ```
3. Read the JSON output. If DICE hangs/errors (the known-untested `viewer.orders` path), copy the error to the billetterie chat to tune.
   - Tokens: paste them inline for a one-off test, OR set as Codespaces secrets. NEVER commit them.

### B. RUN IT ONGOING — GitHub Actions (scheduled, no terminal ever)
Once proven, the fetch runs in the cloud on a schedule:
- Tokens stored as **GitHub Secrets** (repo ▸ Settings ▸ Secrets and variables ▸ Actions) — `SHOTGUN_TOKEN`, `DICE_TOKEN`. Never in code.
- A workflow runs `python festiflow_fetch.py <event>` and stores the JSON where the drawer can read it.
- **The Actions workflow is NOT in this folder yet** — it should be specced by the billetterie chat (it wrote the script and knows the runtime + how the JSON reaches the consumers: committed to repo? artifact? served?). That's an open deployment decision, not assumed here.

## The unlock
A real, contract-conformant **Bordeaux JSON** (fetched + validated against the agreed schema — all-four-deductions-at-every-level, `by_ticket_type_and_day` authoritative grain, comparison block populated) is what hands off to the drawer-design session. That JSON becomes the design chat's render source. Producing it is the goal of the first proven run.

## Secrets — the one hard rule
DICE/Shotgun tokens are secrets. Never hardcoded, never committed, never pasted into a file that gets pushed. Codespaces secrets or GitHub Actions Secrets only. The `.gitignore` guards the common slips, but it's on you not to commit a token inline.
