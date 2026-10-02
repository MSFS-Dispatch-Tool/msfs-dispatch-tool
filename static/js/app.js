// VirtualDispatch app (served as a static file; server values come from
// window.VD_CONFIG, set by templates/index.html)
const STORAGE_KEY = 'simdispatch_active_flight';
const SIMBRIEF_USERNAME_KEY = 'simdispatch_simbrief_username';
const CURRENT_USER_EMAIL = VD_CONFIG.currentUserEmail;
const COUNTRIES = VD_CONFIG.countries;
// Full IATA delay-code list (AHM 730), for coding the pilot's own delays
const IATA_DELAY_CODES = VD_CONFIG.iataDelayCodes;
const MIN_TURNAROUND_MINUTES = VD_CONFIG.minTurnaroundMinutes;
// Departure delays of this many minutes or more must be coded in the PIREP
const DELAY_CODE_THRESHOLD_MINUTES = 3;
const TRIP_TYPE_LABELS = { RT: 'ML', '1W': '1L' };

function simbriefNoticeHtml() {
  if (localStorage.getItem(SIMBRIEF_USERNAME_KEY)) return '';
  return `<div class="notice-banner">SimBrief connection missing. <a href="#" onclick="renderAccountSettings(); return false;">Add your SimBrief username</a> to send loadsheets and flight plans</div>`;
}

function updateClock() {
  const now = new Date();
  const hh = String(now.getUTCHours()).padStart(2, '0');
  const mm = String(now.getUTCMinutes()).padStart(2, '0');
  const ss = String(now.getUTCSeconds()).padStart(2, '0');
  document.getElementById('clock').textContent = `${hh}:${mm}:${ss}Z`;
}
updateClock();
setInterval(updateClock, 1000);

function fmtMinutes(min) {
  if (min === null || min === undefined || Number.isNaN(min)) return 'N/A';
  const h = Math.floor(min / 60);
  const m = min % 60;
  return `${h}h ${String(m).padStart(2, '0')}m`;
}

function fmtNum(value) {
  if (value === null || value === undefined || value === '') return 'N/A';
  const num = Number(value);
  if (!Number.isFinite(num)) return 'N/A';
  return num.toLocaleString('en-US');
}

function escAttr(value) {
  return String(value || '').replace(/"/g, '&quot;');
}

// Opens on the active flight while it still has legs to fly, otherwise on
// flight search. The shared copy is fetched first (briefly), so a flight
// confirmed on another device opens here too.
async function boot() {
  document.getElementById('mainContent').innerHTML = `<div class="panel"><div class="panel-body"><div class="empty-state"><span class="spinner" aria-hidden="true"></span>LOADING&hellip;</div></div></div>`;
  await Promise.race([pullActiveFlight(), new Promise(resolve => setTimeout(resolve, 4000))]);
  updateActiveFlightButtonState();
  const active = loadActiveFlight();
  if (active && isFlightLive(active)) renderRecap(active);
  else renderSearchUI();
}

function isFlightLive(flight) {
  return Array.isArray(flight.leg_status) && flight.leg_status.some(s => s.status !== 'done');
}

function updateActiveFlightButtonState() {
  const btn = document.getElementById('activeFlightBtn');
  if (!btn) return;
  btn.classList.toggle('is-empty', !loadActiveFlight());
}

// ---- Active flight: a working copy in this browser (instant, works
// offline) kept in sync with the account's copy on the server, so a flight
// confirmed on one device continues on any other. Every write carries the
// server revision it was based on; if another device saved in between, the
// server keeps its newer copy and this browser adopts it. ----
const ACTIVE_REV_KEY = 'simdispatch_active_flight_rev';     // server revision the local copy matches
const ACTIVE_DIRTY_KEY = 'simdispatch_active_flight_dirty'; // local change not yet on the server
const ACTIVE_OWNER_KEY = 'simdispatch_active_flight_owner'; // account the local copy belongs to
let activeChangeSeq = 0;
let activePushTimer = null;
let activePushing = false;
let activeSyncNotice = '';

function loadActiveFlight() {
  try {
    const owner = localStorage.getItem(ACTIVE_OWNER_KEY);
    // A copy left behind by a different account in this browser is not ours.
    if (owner && CURRENT_USER_EMAIL && owner !== CURRENT_USER_EMAIL) return null;
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (e) { return null; }
}
function writeLocalActiveFlight(flight) {
  if (flight) localStorage.setItem(STORAGE_KEY, JSON.stringify(flight));
  else localStorage.removeItem(STORAGE_KEY);
  if (CURRENT_USER_EMAIL) localStorage.setItem(ACTIVE_OWNER_KEY, CURRENT_USER_EMAIL);
}
function saveActiveFlight(flight) {
  writeLocalActiveFlight(flight);
  markActiveFlightChanged();
  updateActiveFlightButtonState();
}
function clearActiveFlight() {
  writeLocalActiveFlight(null);
  markActiveFlightChanged();
  updateActiveFlightButtonState();
}

function localActiveRev() { return Number(localStorage.getItem(ACTIVE_REV_KEY) || 0); }

function markActiveFlightChanged() {
  activeChangeSeq++;
  localStorage.setItem(ACTIVE_DIRTY_KEY, '1');
  clearTimeout(activePushTimer);
  activePushTimer = setTimeout(pushActiveFlight, 300);
}

function adoptServerActiveFlight(data) {
  writeLocalActiveFlight(data.flight || null);
  localStorage.setItem(ACTIVE_REV_KEY, String(data.rev));
  localStorage.removeItem(ACTIVE_DIRTY_KEY);
  updateActiveFlightButtonState();
}

async function pushActiveFlight(keepalive) {
  if (!localStorage.getItem(ACTIVE_DIRTY_KEY)) return;
  if (activePushing) { clearTimeout(activePushTimer); activePushTimer = setTimeout(pushActiveFlight, 500); return; }
  activePushing = true;
  const seq = activeChangeSeq;
  try {
    const resp = await fetch('/active-flight', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, keepalive: !!keepalive,
      body: JSON.stringify({ flight: loadActiveFlight(), base_rev: localActiveRev() }),
    });
    const data = await resp.json();
    if (resp.ok) {
      localStorage.setItem(ACTIVE_REV_KEY, String(data.rev));
      if (seq === activeChangeSeq) localStorage.removeItem(ACTIVE_DIRTY_KEY);
    } else if (resp.status === 409) {
      // Another device saved first: its copy wins; show it.
      adoptServerActiveFlight(data);
      activeSyncNotice = 'This flight was updated on another device, so you are seeing the latest version';
      refreshActiveFlightView();
    }
  } catch (e) {
    // Offline or server unavailable: the change stays marked and is sent
    // with the next save or when the app is next opened/focused.
  } finally {
    activePushing = false;
    if (localStorage.getItem(ACTIVE_DIRTY_KEY) && seq !== activeChangeSeq) pushActiveFlight();
  }
}

// Returns true when the local copy was replaced by a newer server copy.
async function pullActiveFlight() {
  try {
    const resp = await fetch('/active-flight');
    if (!resp.ok) return false;
    const data = await resp.json();
    const dirty = !!localStorage.getItem(ACTIVE_DIRTY_KEY);
    const local = loadActiveFlight();
    if (data.rev === localActiveRev()) {
      // Nothing new on the server. Send any unsent local change, including
      // a flight started before syncing existed (nothing saved server-side).
      if (dirty || (data.rev === 0 && local)) { localStorage.setItem(ACTIVE_DIRTY_KEY, '1'); await pushActiveFlight(); }
      return false;
    }
    adoptServerActiveFlight(data);
    return true;
  } catch (e) { return false; }
}

function refreshActiveFlightView() {
  if (currentAppSection !== 'flight') return;
  const flight = loadActiveFlight();
  if (flight) renderRecap(flight); else renderSearchUI();
}

document.addEventListener('visibilitychange', async () => {
  if (document.visibilityState === 'hidden') { pushActiveFlight(true); return; }
  if (await pullActiveFlight()) {
    activeSyncNotice = 'This flight was updated on another device, so you are seeing the latest version';
    refreshActiveFlightView();
  }
});

function getSimbriefUsername(forcePrompt) {
  let username = localStorage.getItem(SIMBRIEF_USERNAME_KEY) || '';
  if (!username || forcePrompt) {
    const entered = prompt('SimBrief username:', username);
    if (entered === null) return username || null;
    username = entered.trim();
    if (username) localStorage.setItem(SIMBRIEF_USERNAME_KEY, username);
  }
  return username || null;
}

function attachAirportDropdown(inputId, dropdownId, endpoint) {
  const input = document.getElementById(inputId);
  const dropdown = document.getElementById(dropdownId);
  endpoint = endpoint || '/airports/search';
  let items = [];
  let activeIndex = -1;
  let querySeq = 0;

  async function query(q) {
    const seq = ++querySeq;
    if (!q) { dropdown.classList.remove('open'); return; }
    const resp = await fetch(`${endpoint}?q=${encodeURIComponent(q)}`);
    const result = await resp.json();
    // A value committed (change event) while this was in flight wins -
    // don't pop the list back open over it.
    if (seq !== querySeq) return;
    items = result;
    renderItems();
  }

  function renderItems() {
    if (items.length === 0) { dropdown.classList.remove('open'); return; }
    dropdown.innerHTML = items.map((a, i) => `
      <div class="opt${i === activeIndex ? ' active' : ''}" data-icao="${a.icao}">
        <span class="code">${a.icao} / ${a.iata}</span>
        <span class="place">${a.city}</span>
      </div>`).join('');
    dropdown.classList.add('open');
    dropdown.querySelectorAll('.opt').forEach((el, i) => {
      el.addEventListener('mousedown', (e) => { e.preventDefault(); selectItem(i); });
    });
  }

  function selectItem(i) {
    input.value = items[i].icao;
    dropdown.classList.remove('open');
    input.dispatchEvent(new Event('change'));
  }

  input.addEventListener('input', (e) => { activeIndex = -1; query(e.target.value); });
  input.addEventListener('change', () => { querySeq++; dropdown.classList.remove('open'); });
  input.addEventListener('keydown', (e) => {
    if (!dropdown.classList.contains('open')) return;
    if (e.key === 'ArrowDown') { activeIndex = Math.min(activeIndex + 1, items.length - 1); renderItems(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { activeIndex = Math.max(activeIndex - 1, 0); renderItems(); e.preventDefault(); }
    else if (e.key === 'Enter' && activeIndex >= 0) { selectItem(activeIndex); e.preventDefault(); }
    else if (e.key === 'Escape') { dropdown.classList.remove('open'); }
  });
  document.addEventListener('click', (e) => {
    if (!dropdown.contains(e.target) && e.target !== input) dropdown.classList.remove('open');
  });
}

// ---------------------------------------------------------------------
// Flight selection. One shared searchState drives the fields, the
// criteria bar, the route map and the itinerary table. Only the two
// airports go to the server (/search); airline, sector legs, flight
// country and the map's route focus are applied client-side, so
// changing any of them is instant, never refetches, and never resets
// the others.
// ---------------------------------------------------------------------
const LEG_FILTER_LABELS = { all: 'ALL', '1W': 'ONE-LEG ONLY', RT: 'MULTI-LEG ONLY' };
const COUNTRY_FILTER_LABELS = { all: 'ALL', domestic: 'DOMESTIC ONLY', international: 'INTERNATIONAL ONLY' };
// Time-of-day windows (UTC) for the first departure and the last arrival
const TIME_FILTER_LABELS = { all: 'ANY TIME', night: '00–06 NIGHT', morning: '06–12 MORNING', afternoon: '12–18 AFTERNOON', evening: '18–24 EVENING' };
const TIME_WINDOWS = { night: [0, 6], morning: [6, 12], afternoon: [12, 18], evening: [18, 24] };
function inTimeWindow(zulu, windowKey) {
  if (windowKey === 'all') return true;
  const h = parseInt(String(zulu || '').slice(0, 2), 10);
  const [lo, hi] = TIME_WINDOWS[windowKey] || [0, 24];
  return Number.isFinite(h) && h >= lo && h < hi;
}
const ROUTE_KEY_RE = /^[A-Z0-9]{4}-[A-Z0-9]{4}$/;

function defaultSearchState() {
  return { origin: '', destination: '', airline: '', legs: 'all', country: 'all', depTime: 'all', arrTime: 'all', route: '' };
}
let searchState = defaultSearchState();
let searchStatus = 'idle';   // idle (no airport) | loading | ready | error
let searchError = '';
let searchAbort = null;
let mapPickMode = 'ask';     // ask | origin | destination
let mapOpenOnMobile = false;
let networkData = null;
let networkPromise = null;

// Greeting on top of Flight search while no flight is live. {part} is the
// pilot's local time of day; {name} their first name (left out if unset).
const GREETINGS = [
  'Good {part}, Captain {name}. Where are we headed today?',
  'Good {part}, Captain. Where to today?',
  'Welcome back, Captain {name}. Which way are we flying today?',
  'Good {part}, Captain {name}. The aircraft is waiting: where are we taking her?',
  "Good {part}, Captain. What does today's roster look like?",
  "Welcome aboard, Captain {name}. Pick a route and we'll get the briefing ready",
  'Good {part}, Captain {name}. Ready to plan your next departure?',
  'Good {part}, Captain {name}. Dispatch is standing by. Where are we headed?',
  'Good {part}, Captain. Which city are we visiting today?',
  'Nice to see you, Captain {name}. Short hop or a long sector today?',
  "Good {part}, Captain {name}. Coffee's on. Where are we flying?",
  'Good {part}, Captain. The briefing room is open. Where to?',
  "Welcome back, Captain {name}. Let's find you a flight",
  'Good {part}, Captain {name}. Your crew is ready. Where are we going today?',
  "Good {part}, Captain. What's the destination today?",
  'Ready when you are, Captain {name}. Where are we headed?',
  "Good {part}, Captain {name}. Let's get you off blocks. Where to?",
];
const LATE_NIGHT_GREETINGS = [   // 22:00-05:00 only
  'Burning the midnight oil, Captain {name}? Where are we headed tonight?',
  'A late one tonight, Captain. Where to?',
];
const GREETING_LAST_KEY = 'vd_last_greeting';

function pickGreeting(firstName, now = new Date()) {
  const hour = now.getHours();
  const part = hour < 12 ? 'morning' : hour < 18 ? 'afternoon' : 'evening';
  const pool = (hour >= 22 || hour < 5) ? GREETINGS.concat(LATE_NIGHT_GREETINGS) : GREETINGS;
  let last = null;
  try { last = localStorage.getItem(GREETING_LAST_KEY); } catch (e) { /* storage blocked */ }
  const choices = pool.filter(t => t !== last);   // never the same one twice in a row
  const template = choices[Math.floor(Math.random() * choices.length)];
  try { localStorage.setItem(GREETING_LAST_KEY, template); } catch (e) { /* storage blocked */ }
  const name = (firstName || '').trim();
  return template.replace('{part}', part).replace(' {name}', name ? ' ' + name : '');
}

function selectOptionsHtml(labels, current) {
  return Object.entries(labels).map(([v, l]) => `<option value="${v}"${v === current ? ' selected' : ''}>${l}</option>`).join('');
}

async function renderSearchUI() {
  setAppNav('search');
  if (selMap) { selMap.remove(); selMap = null; }
  const fromUrl = readSearchUrl();
  searchState = fromUrl || defaultSearchState();
  currentItineraries = [];
  searchStatus = 'idle';
  expandedKey = null; expandedDetailData = null; expandedDetailKey = null;
  hoverKey = null; pinnedKey = null;
  setPageFit(true);
  const settingsPromise = fetch('/settings').then(r => r.json()).catch(() => null);
  const activeFlight = loadActiveFlight();
  const showGreeting = !(activeFlight && isFlightLive(activeFlight));

  document.getElementById('mainContent').innerHTML = `
    ${showGreeting ? '<h1 class="greeting" id="greeting"></h1>' : ''}
    ${simbriefNoticeHtml()}
    <div class="search-layout">
    <div class="search-main">
    <div class="panel params-panel">
      <div class="panel-header">SEARCH PARAMETERS</div>
      <div class="panel-body">
        <div class="params-grid cols-3">
          <div class="field">
            <label for="origin">DEPARTURE AIRPORT <span class="hint">(ICAO/IATA)</span></label>
            <input type="text" id="origin" autocomplete="off" placeholder="e.g. BGY or LIME">
            <div class="custom-dropdown" id="originDropdown"></div>
            <div class="field-error" id="originError">UNKNOWN AIRPORT CODE &ndash; NOT APPLIED</div>
          </div>
          <div class="field">
            <label for="destination">DESTINATION AIRPORT <span class="hint">(ICAO/IATA)</span></label>
            <input type="text" id="destination" autocomplete="off" placeholder="e.g. STN or EGSS">
            <div class="custom-dropdown" id="destinationDropdown"></div>
            <div class="field-error" id="destinationError">UNKNOWN AIRPORT CODE &ndash; NOT APPLIED</div>
          </div>
          <div class="field">
            <label for="legsFilter">SECTOR LEGS</label>
            <select id="legsFilter" onchange="setSearchFilter('legs', this.value)">${selectOptionsHtml(LEG_FILTER_LABELS, searchState.legs)}</select>
          </div>
          <div class="field">
            <label for="airlineFilter">AIRLINE</label>
            <select id="airlineFilter" onchange="setSearchFilter('airline', this.value)">
              <option value="">ALL</option>
            </select>
          </div>
          <div class="field">
            <label for="countryFilter">FLIGHT COUNTRY</label>
            <select id="countryFilter" onchange="setSearchFilter('country', this.value)">${selectOptionsHtml(COUNTRY_FILTER_LABELS, searchState.country)}</select>
          </div>
          <div class="field">
            <label for="depTimeFilter">DEPARTS / ARRIVES <span class="hint">(UTC)</span></label>
            <div class="time-filters">
              <select id="depTimeFilter" aria-label="Departure time (UTC)" onchange="setSearchFilter('depTime', this.value)">${selectOptionsHtml(TIME_FILTER_LABELS, searchState.depTime)}</select>
              <select id="arrTimeFilter" aria-label="Arrival time (UTC)" onchange="setSearchFilter('arrTime', this.value)">${selectOptionsHtml(TIME_FILTER_LABELS, searchState.arrTime)}</select>
            </div>
          </div>
        </div>
        <p class="field-note" id="searchRequirementNote">Type a departure and/or destination airport, or pick one on the map. Sector legs, airline, flight country and times apply instantly</p>
        <div class="params-actions">
          <button class="action" id="searchBtn" onclick="applyTypedAirports()">SEARCH</button>
          <button class="ghost" type="button" id="swapBtn" onclick="swapAirports()" title="Swap departure and destination">&#8644; SWAP</button>
          <button class="ghost" type="button" onclick="resetSearch()">RESET ALL</button>
        </div>
      </div>
    </div>
    <div class="panel criteria-panel">
      <div class="criteria-bar" id="criteriaBar"></div>
    </div>
    <div class="panel" id="mapPanel">
      <div class="panel-header">
        <span class="map-title">ROUTE MAP</span>
        <button class="ghost small map-toggle-btn" type="button" id="mapToggleBtn" aria-controls="mapBody" aria-expanded="false" onclick="toggleMapOnMobile()">SHOW MAP</button>
      </div>
      <div class="panel-body map-body" id="mapBody">
        <div class="map-toolbar">
          <fieldset class="seg-control">
            <legend>CLICK AN AIRPORT TO</legend>
            <label><input type="radio" name="mapPickMode" value="ask" checked onchange="setMapPickMode(this.value)"><span>CHOOSE EACH TIME</span></label>
            <label><input type="radio" name="mapPickMode" value="origin" onchange="setMapPickMode(this.value)"><span>SET DEPARTURE</span></label>
            <label><input type="radio" name="mapPickMode" value="destination" onchange="setMapPickMode(this.value)"><span>SET DESTINATION</span></label>
          </fieldset>
          <button class="ghost small" type="button" onclick="fitSelectionMap()">FIT TO RESULTS</button>
        </div>
        <div class="wx-toolbar">
          <span class="wx-title">WEATHER</span>
          <label class="switch-row"><span class="switch-toggle"><input type="checkbox" id="wxCatToggle" onchange="setWeatherLayer('cat', this.checked)"><span class="track"></span></span>FLIGHT CATEGORY</label>
          <label class="switch-row"><span class="switch-toggle"><input type="checkbox" id="wxSigmetToggle" onchange="setWeatherLayer('sigmet', this.checked)"><span class="track"></span></span>SIGMETS</label>
          <span class="wx-status" id="wxStatus" aria-live="polite"></span>
        </div>
        <div class="selection-map-wrap">
          <div id="selectionMap" class="selection-map" role="region" aria-label="Route map. A visual aid: every search option is also available in the fields above, and every result in the flight list"></div>
          <div class="map-overlay" id="mapOverlay" hidden></div>
        </div>
        <div class="map-legend" aria-hidden="true">
          <span><i class="sw sw-dot" style="background:#1D4ED8"></i>DEPARTURE</span>
          <span><i class="sw sw-dot" style="background:#C4320A"></i>DESTINATION</span>
          <span><i class="sw sw-dot" style="background:#344054"></i>AIRPORT WITH ITINERARIES</span>
          <span><i class="sw sw-dot sw-ring"></i>MULTI-LEG TURNAROUND (VIA) ONLY</span>
          <span><i class="sw sw-line"></i>ROUTE</span>
          <span><i class="sw sw-line hl"></i>ROW YOU'RE POINTING AT</span>
        </div>
        <div class="map-legend wx-legend" id="wxLegend" aria-hidden="true" hidden></div>
        <p class="map-hint">Panning and zooming never change the search. Click a route line to show only its itineraries in the list</p>
      </div>
    </div>
    </div>
    <div class="search-side" id="listPanel"></div>
    </div>
  `;
  sizeSearchLayout();
  // The mouse wheel zooms the map only while all of it is on screen;
  // otherwise it keeps scrolling the page, so scrolling down to the map
  // never gets caught zooming it halfway.
  document.querySelector('.selection-map-wrap').addEventListener('wheel', (e) => {
    if (!selectionMapFullyInView()) e.stopPropagation();
  }, { capture: true });
  if (showGreeting) {
    settingsPromise.then(settings => {
      const el = document.getElementById('greeting');
      if (!el) return;
      el.textContent = pickGreeting(settings && settings.profile && settings.profile.first_name);
      sizeSearchLayout();
    });
  }
  attachAirportDropdown('origin', 'originDropdown');
  attachAirportDropdown('destination', 'destinationDropdown');
  ['origin', 'destination'].forEach(role => {
    const input = document.getElementById(role);
    input.value = searchState[role];
    // "change" covers both a dropdown pick (attachAirportDropdown fires it)
    // and a typed code once the field loses focus.
    input.addEventListener('change', () => commitAirportField(role));
    input.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      const dropdown = document.getElementById(role + 'Dropdown');
      // A highlighted suggestion is picked by attachAirportDropdown itself.
      if (dropdown.classList.contains('open') && dropdown.querySelector('.opt.active')) return;
      e.preventDefault();
      input.dispatchEvent(new Event('change'));
    });
  });

  // Prefill from the pilot's preferred base (set during onboarding /
  // account settings) unless the URL already carries a search.
  if (!fromUrl) {
    try {
      const settings = await settingsPromise;
      const base = settings.profile && settings.profile.preferred_base;
      if (base) {
        const icao = await resolveAirportCode(base);
        if (icao) { searchState.origin = icao; document.getElementById('origin').value = icao; }
      }
    } catch (err) { /* non-critical - search still works without a prefill */ }
  }

  try {
    const carriers = await getCarriers();
    const select = document.getElementById('airlineFilter');
    carriers.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.icao;
      opt.textContent = c.name.toUpperCase();
      select.appendChild(opt);
    });
    if (searchState.airline && !carriers.some(c => c.icao === searchState.airline)) searchState.airline = '';
    select.value = searchState.airline;
  } catch (err) { searchState.airline = ''; }

  // While a search is in flight its own result render does the fit.
  getNetwork().then(() => renderSelection({ fit: searchStatus !== 'loading' })).catch(() => renderSelection({ fit: false }));
  initWeather();
  runSearch();
}

// ---- Search state: URL, fields, filters ----

function syncSearchUrl() {
  const s = searchState;
  const p = new URLSearchParams();
  if (s.origin) p.set('from', s.origin);
  if (s.destination) p.set('to', s.destination);
  if (s.airline) p.set('airline', s.airline);
  if (s.legs !== 'all') p.set('legs', s.legs);
  if (s.country !== 'all') p.set('country', s.country);
  if (s.depTime !== 'all') p.set('dep', s.depTime);
  if (s.arrTime !== 'all') p.set('arr', s.arrTime);
  if (s.route) p.set('route', s.route);
  const qs = p.toString();
  history.replaceState(null, '', location.pathname + (qs ? '?' + qs : ''));
}

function readSearchUrl() {
  const p = new URLSearchParams(location.search);
  if (![...p.keys()].some(k => ['from', 'to', 'airline', 'legs', 'country', 'dep', 'arr', 'route'].includes(k))) return null;
  const code = v => (v || '').trim().toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 4);
  const s = defaultSearchState();
  s.origin = code(p.get('from'));
  s.destination = code(p.get('to'));
  s.airline = code(p.get('airline'));
  if (p.get('legs') in LEG_FILTER_LABELS) s.legs = p.get('legs');
  if (p.get('country') in COUNTRY_FILTER_LABELS) s.country = p.get('country');
  if (p.get('dep') in TIME_WINDOWS) s.depTime = p.get('dep');
  if (p.get('arr') in TIME_WINDOWS) s.arrTime = p.get('arr');
  const route = (p.get('route') || '').toUpperCase();
  if (ROUTE_KEY_RE.test(route)) s.route = route;
  return s;
}

async function resolveAirportCode(code) {
  try {
    const result = await (await fetch(`/airports/validate?code=${encodeURIComponent(code)}`)).json();
    return result.valid ? result.icao : null;
  } catch (err) { return null; }
}

async function commitAirportField(role) {
  const field = document.getElementById(role);
  if (!field) return;
  const errorEl = document.getElementById(role + 'Error');
  const raw = field.value.trim();
  if (!raw) {
    errorEl.classList.remove('show');
    setAirport(role, '');
    return;
  }
  const icao = await resolveAirportCode(raw);
  if (field.value.trim() !== raw) return;   // user kept typing meanwhile
  if (!icao) {
    // Keep the last applied airport in the search; the error says the
    // typed value was not applied, so the criteria bar never disagrees
    // with the results it describes.
    errorEl.classList.add('show');
    return;
  }
  errorEl.classList.remove('show');
  field.value = icao;
  setAirport(role, icao);
}

async function applyTypedAirports() {
  await Promise.all(['origin', 'destination'].map(commitAirportField));
}

function setFieldValue(role, icao) {
  const field = document.getElementById(role);
  if (field) field.value = icao;
  const errorEl = document.getElementById(role + 'Error');
  if (errorEl) errorEl.classList.remove('show');
}

function setAirport(role, icao) {
  if (searchState[role] === icao) return;
  searchState[role] = icao;
  setFieldValue(role, icao);
  runSearch();
}

// Map clicks move an airport between roles instead of producing a
// same-airport-both-ends search: picking the current destination as the
// departure clears it from the destination.
function mapSetAirport(role, icao) {
  if (selMap) selMap.closePopup();
  const other = role === 'origin' ? 'destination' : 'origin';
  let changed = false;
  if (searchState[other] === icao) { searchState[other] = ''; setFieldValue(other, ''); changed = true; }
  if (searchState[role] !== icao) { searchState[role] = icao; setFieldValue(role, icao); changed = true; }
  if (changed) runSearch();
}

function swapAirports() {
  if (selMap) selMap.closePopup();
  const s = searchState;
  if (!s.origin && !s.destination) return;
  [s.origin, s.destination] = [s.destination, s.origin];
  setFieldValue('origin', s.origin);
  setFieldValue('destination', s.destination);
  runSearch();
}

function setSearchFilter(name, value) {
  if (searchState[name] === value) return;
  searchState[name] = value;
  const ids = { legs: 'legsFilter', airline: 'airlineFilter', country: 'countryFilter', depTime: 'depTimeFilter', arrTime: 'arrTimeFilter' };
  if (ids[name]) { const el = document.getElementById(ids[name]); if (el && el.value !== value) el.value = value; }
  syncSearchUrl();
  renderSelection({ fit: false });
}

function toggleRouteFocus(routeKey) {
  searchState.route = searchState.route === routeKey ? '' : routeKey;
  syncSearchUrl();
  renderSelection({ fit: false });
}

function resetSearch() {
  if (selMap) selMap.closePopup();
  searchState = defaultSearchState();
  ['origin', 'destination'].forEach(r => setFieldValue(r, ''));
  document.getElementById('legsFilter').value = 'all';
  document.getElementById('airlineFilter').value = '';
  document.getElementById('countryFilter').value = 'all';
  document.getElementById('depTimeFilter').value = 'all';
  document.getElementById('arrTimeFilter').value = 'all';
  runSearch();
}

function resetResultFilters() {
  searchState.airline = ''; searchState.legs = 'all'; searchState.country = 'all'; searchState.route = '';
  searchState.depTime = 'all'; searchState.arrTime = 'all';
  document.getElementById('legsFilter').value = 'all';
  document.getElementById('airlineFilter').value = '';
  document.getElementById('countryFilter').value = 'all';
  document.getElementById('depTimeFilter').value = 'all';
  document.getElementById('arrTimeFilter').value = 'all';
  syncSearchUrl();
  renderSelection({ fit: false });
}

function setMapPickMode(mode) {
  mapPickMode = mode;
  if (selMap) selMap.closePopup();
  const el = document.getElementById('selectionMap');
  if (el) el.classList.toggle('pick-direct', mode !== 'ask');
}

function toggleMapOnMobile() {
  mapOpenOnMobile = !mapOpenOnMobile;
  applyMapPanelOpenState();
  if (mapOpenOnMobile && selMap) { selMap.invalidateSize(); fitSelectionMap(); }
}

function applyMapPanelOpenState() {
  const panel = document.getElementById('mapPanel');
  const btn = document.getElementById('mapToggleBtn');
  if (!panel || !btn) return;
  panel.classList.toggle('map-open', mapOpenOnMobile);
  btn.setAttribute('aria-expanded', mapOpenOnMobile ? 'true' : 'false');
  btn.textContent = mapOpenOnMobile ? 'HIDE MAP' : 'SHOW MAP';
}

let currentItineraries = [];
let expandedKey = null;
let expandedDetailData = null;
let pirepLogRecords = [];
let expandedPirepId = null;
let currentSettings = null;
let carriersCache = null;

async function getCarriers() {
  if (!carriersCache) carriersCache = await (await fetch('/carriers')).json();
  return carriersCache;
}

async function getOwnedAircraft() {
  if (!currentSettings) currentSettings = await (await fetch('/settings')).json();
  return new Set(currentSettings.profile.aircraft_owned || []);
}

let expandedDetailKey = null;   // which itinerary expandedDetailData was rolled for
let detailLoadingKey = null;
let hoverKey = null;            // table row under the pointer / keyboard focus
let pinnedKey = null;           // row whose "show on map" was pressed
let hoverSuppressedAt = null;   // pointer position at that press

async function runSearch() {
  syncSearchUrl();
  if (searchAbort) { searchAbort.abort(); searchAbort = null; }
  expandedKey = null; expandedDetailData = null; expandedDetailKey = null; detailLoadingKey = null;
  hoverKey = null; pinnedKey = null;

  if (!searchState.origin && !searchState.destination) {
    currentItineraries = [];
    searchStatus = 'idle';
    renderSelection({ fit: true });
    return;
  }

  searchStatus = 'loading';
  renderSelection({ fit: false });
  const ctrl = new AbortController();
  searchAbort = ctrl;
  try {
    const params = new URLSearchParams({ origin: searchState.origin, destination: searchState.destination, trip_type: 'random' });
    const data = await (await fetch(`/search?${params}`, { signal: ctrl.signal })).json();
    if (searchAbort !== ctrl) return;
    if (data.error) {
      currentItineraries = []; searchStatus = 'error'; searchError = data.error.toUpperCase();
    } else {
      currentItineraries = data.itineraries;
      searchStatus = 'ready';
      // A route focus survives an airport change only if that route is
      // still part of the new results.
      if (searchState.route && !currentItineraries.some(i => itinRouteKey(i) === searchState.route)) {
        searchState.route = '';
        syncSearchUrl();
      }
    }
  } catch (err) {
    if (err.name === 'AbortError') return;
    currentItineraries = []; searchStatus = 'error'; searchError = 'SEARCH FAILED. CHECK THE CONNECTION AND RETRY';
  }
  if (searchAbort === ctrl) searchAbort = null;
  renderSelection({ fit: true });
}

// The geographic segment an itinerary is drawn on: a round trip A->X->A
// flies the A-X segment twice, a one-leg flight its own two airports.
function routeKeyFor(a, b) { return [a, b].sort().join('-'); }
function itinRouteKey(itin) { return routeKeyFor(itin.departure_icao, itin.via_icao || itin.arrival_icao); }

function passesFilters(itin, skip) {
  const s = searchState;
  if (skip !== 'airline' && s.airline && itin.carrier !== s.airline) return false;
  if (skip !== 'legs' && s.legs !== 'all' && itin.trip_type !== s.legs) return false;
  if (skip !== 'country' && s.country === 'domestic' && !itin.domestic) return false;
  if (skip !== 'country' && s.country === 'international' && itin.domestic) return false;
  if (skip !== 'route' && s.route && itinRouteKey(itin) !== s.route) return false;
  if (skip !== 'depTime' && !inTimeWindow(itin.first_departure_zulu, s.depTime)) return false;
  if (skip !== 'arrTime' && !inTimeWindow(itin.last_arrival_zulu, s.arrTime)) return false;
  return true;
}

function filteredItineraries(skip) {
  return currentItineraries.filter(i => passesFilters(i, skip));
}

function airportLabel(icao) {
  const a = networkData && networkData.airportsByIcao[icao];
  return a ? `${icao} (${a.city.toUpperCase()})` : icao;
}

function carrierName(code) {
  const c = (carriersCache || []).find(c => c.icao === code);
  return c ? c.name.toUpperCase() : code;
}

// Everything the criteria bar can show, in display order, each with the
// action that removes it - the bar and the no-results suggestions share it.
function activeCriteria() {
  const s = searchState;
  const list = [];
  if (s.origin) list.push({ id: 'origin', cls: 'dep', k: 'FROM', v: airportLabel(s.origin), clear: "setAirport('origin','')" });
  if (s.destination) list.push({ id: 'destination', cls: 'dest', k: 'TO', v: airportLabel(s.destination), clear: "setAirport('destination','')" });
  if (s.airline) list.push({ id: 'airline', k: 'AIRLINE', v: carrierName(s.airline), clear: "setSearchFilter('airline','')" });
  if (s.legs !== 'all') list.push({ id: 'legs', k: 'LEGS', v: LEG_FILTER_LABELS[s.legs], clear: "setSearchFilter('legs','all')" });
  if (s.country !== 'all') list.push({ id: 'country', k: 'COUNTRY', v: COUNTRY_FILTER_LABELS[s.country], clear: "setSearchFilter('country','all')" });
  if (s.depTime !== 'all') list.push({ id: 'depTime', k: 'DEPARTS', v: TIME_FILTER_LABELS[s.depTime] + ' UTC', clear: "setSearchFilter('depTime','all')" });
  if (s.arrTime !== 'all') list.push({ id: 'arrTime', k: 'ARRIVES', v: TIME_FILTER_LABELS[s.arrTime] + ' UTC', clear: "setSearchFilter('arrTime','all')" });
  if (s.route) list.push({ id: 'route', k: 'ROUTE', v: s.route.replace('-', ' – '), clear: "toggleRouteFocus(searchState.route)" });
  return list;
}

// Drawn twice: as its own bar (phones, tablets) and inside the list's box
// (desktop, where the list sits beside the filters); CSS shows one.
function renderCriteriaBar(visibleCount) {
  const bars = ['criteriaBar', 'listCriteria'].map(id => document.getElementById(id)).filter(Boolean);
  if (!bars.length) return;
  let countHtml;
  if (searchStatus === 'idle') countHtml = `<div class="result-count muted">NO AIRPORT SELECTED</div>`;
  else if (searchStatus === 'loading') countHtml = `<div class="result-count muted"><span class="spinner" aria-hidden="true"></span>SEARCHING&hellip;</div>`;
  else if (searchStatus === 'error') countHtml = `<div class="result-count error">SEARCH ERROR</div>`;
  else countHtml = `<div class="result-count"><strong>${fmtNum(visibleCount)}</strong>${visibleCount === 1 ? 'ITINERARY' : 'ITINERARIES'}</div>`;

  const crit = activeCriteria();
  const chips = crit.length
    ? crit.map((c, i) => `${c.id === 'destination' && searchState.origin ? `<button class="chip-swap" type="button" onclick="swapAirports()" title="Swap departure and destination" aria-label="Swap departure and destination">&#8644;</button>` : ''}<span class="crit-chip ${c.cls || ''}"><span class="k">${c.k}</span>${c.v}<button type="button" onclick="${c.clear}" aria-label="Remove ${c.k.toLowerCase()} ${escAttr(c.v)}" title="Remove">&times;</button></span>`).join('')
    : `<span class="crit-empty">Pick a departure or destination airport to start &mdash; type it above or click it on the map</span>`;

  const html = `
    <div aria-live="polite" aria-atomic="true">${countHtml}</div>
    <div class="crit-chips">${chips}</div>
    ${crit.length ? `<button class="ghost small" type="button" onclick="resetSearch()">RESET ALL</button>` : ''}`;
  bars.forEach(bar => { bar.innerHTML = html; });
}

// Explains an empty result and offers the single changes that would fix
// it, with the itinerary count each one would give - criteria are kept.
function noResultsHtml() {
  const s = searchState;
  const btn = (action, label, disabled) => `<button class="ghost" type="button" onclick="${action}"${disabled ? ' disabled' : ''}>${label}</button>`;
  if (currentItineraries.length === 0) {
    const actions = [];
    let msg;
    if (s.origin && s.origin === s.destination) {
      msg = `Departure and destination are both ${s.origin}.`;
    } else if (s.origin && s.destination) {
      msg = `No scheduled flights go from ${airportLabel(s.origin)} to ${airportLabel(s.destination)} in the loaded network`;
      const reverse = networkData && networkData.pairs.has(`${s.destination}>${s.origin}`);
      if (reverse) actions.push(btn('swapAirports()', `SWAP TO ${s.destination} &rarr; ${s.origin}`));
    } else {
      msg = `No itineraries were found for ${airportLabel(s.origin || s.destination)}.`;
    }
    if (s.destination) actions.push(btn("setAirport('destination','')", `REMOVE DESTINATION${s.origin ? ` &middot; ${fmtNum(networkDestinationCount(s.origin))} DESTINATIONS FROM ${s.origin}` : ''}`));
    if (s.origin) actions.push(btn("setAirport('origin','')", `REMOVE DEPARTURE${s.destination ? ` &middot; ${fmtNum(networkOriginCount(s.destination))} ORIGINS TO ${s.destination}` : ''}`));
    return `<div class="no-results"><h4>NO ITINERARIES MATCH</h4><p>${msg} Your criteria are kept &mdash; relax one to see flights</p><div class="relax-actions">${actions.join('')}</div></div>`;
  }
  const relaxable = activeCriteria().filter(c => ['airline', 'legs', 'country', 'route'].includes(c.id));
  const suggestions = relaxable.map(c => {
    const n = filteredItineraries(c.id).length;
    return btn(c.clear, `REMOVE ${c.k}: ${c.v} &middot; ${fmtNum(n)} ${n === 1 ? 'ITINERARY' : 'ITINERARIES'}`, n === 0);
  });
  const anySingleFix = relaxable.some(c => filteredItineraries(c.id).length > 0);
  if (!anySingleFix && relaxable.length > 1) suggestions.push(btn('resetResultFilters()', `CLEAR ALL FILTERS &middot; KEEP AIRPORTS (${fmtNum(currentItineraries.length)})`));
  return `<div class="no-results"><h4>NO ITINERARIES MATCH ALL YOUR FILTERS</h4><p>${fmtNum(currentItineraries.length)} itineraries match the airports, but not every filter below. Your criteria are kept &mdash; relax one to see flights:</p><div class="relax-actions">${suggestions.join('')}</div></div>`;
}

function networkDestinationCount(icao) {
  if (!networkData) return 0;
  return networkData.routes.filter(([d]) => networkData.airports[d].icao === icao).length;
}
function networkOriginCount(icao) {
  if (!networkData) return 0;
  return networkData.routes.filter(([, a]) => networkData.airports[a].icao === icao).length;
}

// Single render pass for everything that depends on the search state.
function renderSelection({ fit }) {
  const visible = searchStatus === 'ready' ? filteredItineraries() : [];
  renderListPanel(visible);
  renderCriteriaBar(visible.length);
  sizeSearchLayout();   // the criteria chips can wrap onto more lines
  renderSelectionMap({ fit });
  applyMapPanelOpenState();
}

function itinKey(itin) { return itin.flight_numbers.join(','); }

let sortState = { key: 'flight', dir: 'asc' };

// Every sortable column's header label and comparator live together,
// so adding/removing a sortable column never means touching two
// separate places (the <thead> markup and the comparator table) that
// could drift out of sync.
const SORT_COLUMNS = {
  carrier:   { label: 'AIRLINE',        cmp: (a, b) => a.carrier.localeCompare(b.carrier) },
  // TYPE's badges already say how many legs (ML = 2, 1L = 1), so it
  // sorts by leg count and replaces the old, redundant LEGS column.
  type:      { label: 'TYPE',           cmp: (a, b) => (a.legs - b.legs) || (b.domestic - a.domestic) },
  flight:    { label: 'FLIGHT(S)',      cmp: (a, b) => a.flight_numbers[0].localeCompare(b.flight_numbers[0], undefined, {numeric: true}) },
  departure: { label: 'FROM',           cmp: (a, b) => a.departure_icao.localeCompare(b.departure_icao) },
  arrival:   { label: 'TO',             cmp: (a, b) => a.arrival_icao.localeCompare(b.arrival_icao) },
  dep_time:  { label: 'ETD (Z)',        cmp: (a, b) => (a.first_departure_zulu || '').localeCompare(b.first_departure_zulu || '') },
  arr_time:  { label: 'ETA (Z)',        cmp: (a, b) => (a.last_arrival_zulu || '').localeCompare(b.last_arrival_zulu || '') },
  distance:  { label: 'DISTANCE',       cmp: (a, b) => (a.total_distance_nm ?? -1) - (b.total_distance_nm ?? -1) },
  ebt:       { label: 'EBT',            cmp: (a, b) => (a.ebt_minutes ?? -1) - (b.ebt_minutes ?? -1) },
};

function sortItineraries(list) {
  const sorted = [...list];
  sorted.sort(SORT_COLUMNS[sortState.key].cmp);
  if (sortState.dir === 'desc') sorted.reverse();
  return sorted;
}
function setSort(key, dir) {
  if (dir) {
    sortState.key = key;
    sortState.dir = dir;
  } else if (sortState.key === key) {
    sortState.dir = sortState.dir === 'asc' ? 'desc' : 'asc';
  } else {
    sortState.key = key;
    sortState.dir = 'asc';
  }
  renderTable();
}

// Strips the trailing "Z" (and only it) off a formatted Zulu time like
// "14:30Z" or "14:30Z (+1d)", since the table now says "(Z)" once in
// the column header instead of repeating it in every cell.
function stripZulu(s) {
  return s ? s.replace(/Z\b/, '') : s;
}

function sortHeaderHtml(key) {
  const col = SORT_COLUMNS[key];
  const active = sortState.key === key;
  const arrow = active ? (sortState.dir === 'asc' ? '&uarr;' : '&darr;') : '&uarr;';
  return `<th class="sortable${active ? ' sort-active' : ''}" onclick="setSort('${key}')">${col.label}<span class="sort-arrow">${arrow}</span></th>`;
}

const MAP_PIN_SVG = `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/></svg>`;

let tableVisibleItineraries = [];

function renderListPanel(visible) {
  const panel = document.getElementById('listPanel');
  if (!panel) return;
  tableVisibleItineraries = visible || [];
  let body;
  if (searchStatus === 'idle') {
    body = `<div class="empty-state">Choose a departure or destination airport &mdash; type it in Search Parameters or click it on the map</div>`;
  } else if (searchStatus === 'loading') {
    body = `<div class="empty-state"><span class="spinner" aria-hidden="true"></span>LOADING ITINERARIES&hellip;</div>`;
  } else if (searchStatus === 'error') {
    body = `<div class="no-results"><h4>${searchError}</h4><div class="relax-actions"><button class="ghost" type="button" onclick="runSearch()">RETRY</button></div></div>`;
  } else if (tableVisibleItineraries.length === 0) {
    body = noResultsHtml();
  } else {
    body = `<table class="itin-list" id="itinTable"><thead id="itinTableHead"></thead><tbody id="itinTableBody"></tbody></table>`;
  }
  // The sort menu stands in for the column headers wherever flights are
  // shown as cards or two-line rows; BACK TO LIST shows (desktop only)
  // while one flight's briefing replaces the list.
  const tools = tableVisibleItineraries.length && searchStatus === 'ready' ? `
    <div class="list-tools">
      <label class="sort-by">SORT BY
        <select id="sortBySelect" onchange="setSort(this.value, 'asc')">${Object.entries(SORT_COLUMNS).map(([k, c]) => `<option value="${k}">${c.label}</option>`).join('')}</select>
      </label>
      <button class="ghost small sort-by sort-dir" type="button" id="sortDirBtn" onclick="setSort(sortState.key)"></button>
      <button class="ghost small back-to-list" type="button" onclick="toggleItinerary(expandedKey)">&larr; BACK TO LIST</button>
    </div>` : '';
  panel.innerHTML = `
    <div class="panel list-panel" id="listPanelInner">
      <div class="panel-header"><span>MATCHING ITINERARIES</span>${tools}</div>
      <div class="criteria-bar list-criteria" id="listCriteria"></div>
      <div class="panel-body" id="listScroll">${body}</div>
    </div>`;
  const tbody = document.getElementById('itinTableBody');
  if (!tbody) return;
  // Delegated once per table: hovering or keyboard-focusing a row
  // identifies its route on the map.
  const rowKey = (e) => { const tr = e.target.closest('tr.itin-row'); return tr ? tr.dataset.key : null; };
  tbody.addEventListener('mouseover', (e) => {
    // After "show on map" scrolls the page, rows slide under a pointer
    // that never moved; ignore that until the pointer really moves, or it
    // would replace the pinned itinerary with whatever row landed there.
    if (hoverSuppressedAt) {
      if (e.screenX === hoverSuppressedAt.x && e.screenY === hoverSuppressedAt.y) return;
      hoverSuppressedAt = null;
    }
    hoverItinerary(rowKey(e));
  });
  tbody.addEventListener('mouseleave', () => hoverItinerary(null));
  tbody.addEventListener('focusin', (e) => hoverItinerary(rowKey(e)));
  tbody.addEventListener('focusout', (e) => { if (!tbody.contains(e.relatedTarget)) hoverItinerary(null); });
  renderTable();
}

function renderTable() {
  const head = document.getElementById('itinTableHead');
  const tbody = document.getElementById('itinTableBody');
  if (!head || !tbody) return;
  head.innerHTML = `
    <tr>
      ${sortHeaderHtml('carrier')}
      ${sortHeaderHtml('type')}
      ${sortHeaderHtml('flight')}
      ${sortHeaderHtml('departure')}
      <th>VIA</th>
      ${sortHeaderHtml('arrival')}
      ${sortHeaderHtml('dep_time')}
      ${sortHeaderHtml('arr_time')}
      <th>TURNAROUND</th>
      ${sortHeaderHtml('distance')}
      ${sortHeaderHtml('ebt')}
      <th><span class="sr-only">Actions</span></th>
    </tr>`;

  const sortSelect = document.getElementById('sortBySelect');
  if (sortSelect) sortSelect.value = sortState.key;
  const dirBtn = document.getElementById('sortDirBtn');
  if (dirBtn) {
    const asc = sortState.dir === 'asc';
    dirBtn.innerHTML = asc ? '&uarr;' : '&darr;';
    dirBtn.title = asc ? 'Ascending: click to reverse' : 'Descending: click to reverse';
    dirBtn.setAttribute('aria-label', asc ? 'Sorted ascending. Reverse the order' : 'Sorted descending. Reverse the order');
  }
  const listPanel = document.getElementById('listPanelInner');
  if (listPanel) listPanel.classList.toggle('has-open', !!expandedKey);

  const sorted = sortItineraries(tableVisibleItineraries);
  const colCount = head.querySelectorAll('th').length;
  const highlighted = currentHighlightKey();

  tbody.innerHTML = sorted.map(itin => {
    const key = itinKey(itin);
    const isExpanded = key === expandedKey;
    const rowState = isExpanded ? 'expanded' : (expandedKey ? 'dimmed' : '');
    const domTag = itin.domestic ? `<span class="type-badge type-DOM">DOM</span>` : `<span class="type-badge type-INT">INT</span>`;
    const turnaround = itin.leg_details.length > 1 ? fmtMinutes(itin.leg_details[1].turnaround_minutes) : '&mdash;';
    const flights = itin.flight_numbers.join(' / ');
    const row = `
      <tr class="itin-row ${rowState}${key === highlighted ? ' map-hl' : ''}" data-key="${key}" onclick="toggleItinerary('${key}')">
        <td data-label="AIRLINE"><span class="carrier-badge">${itin.carrier}</span></td>
        <td data-label="TYPE"><div class="type-cell"><span class="type-badge type-${itin.trip_type}">${TRIP_TYPE_LABELS[itin.trip_type] || itin.trip_type}</span>${domTag}</div></td>
        <td data-label="FLIGHT(S)">${flights}</td>
        <td data-label="DEPARTURE">${itin.departure_icao}</td>
        <td data-label="VIA" class="via-cell${itin.via_icao ? '' : ' no-via'}${itin.via_icao && itin.via_icao === searchState.destination ? ' matched-cell" title="Matches your destination: the turnaround airport of this multi-leg rotation' : ''}">${itin.via_icao || '&mdash;'}</td>
        <td data-label="ARRIVAL">${itin.arrival_icao}</td>
        <td data-label="ETD (Z)" data-short="ETD">${itin.first_departure_zulu ? stripZulu(itin.first_departure_zulu) : 'UNKNOWN'}</td>
        <td data-label="ETA (Z)" data-short="ETA">${itin.last_arrival_zulu ? stripZulu(itin.last_arrival_zulu) : 'UNKNOWN'}</td>
        <td data-label="TURNAROUND" data-short="TURN"${itin.leg_details.length > 1 ? '' : ' class="no-turn"'}>${turnaround}</td>
        <td data-label="DISTANCE" class="nowrap">${itin.total_distance_nm != null ? fmtNum(itin.total_distance_nm) + ' nm' : 'N/A'}</td>
        <td data-label="EBT" data-short="EBT">${itin.ebt_minutes != null ? fmtMinutes(itin.ebt_minutes) : 'N/A'}</td>
        <td class="select-cell" data-label=""><button class="ghost row-map-btn" type="button" onclick="event.stopPropagation(); showItineraryOnMap('${key}', event)" title="Show on map" aria-label="Show ${flights} on the map">${MAP_PIN_SVG}</button><button class="action small" onclick="event.stopPropagation(); toggleItinerary('${key}')" aria-expanded="${isExpanded}">${isExpanded ? 'CLOSE' : 'SELECT'}</button></td>
      </tr>`;
    const detailRow = isExpanded
      ? `<tr><td colspan="${colCount}" class="detail-cell" id="detailCell"><div class="empty-state">LOADING...</div></td></tr>`
      : '';
    return row + detailRow;
  }).join('');

  // Re-rendering (sorting, filtering, a map click) must never re-roll the
  // open flight: /select randomizes delay/MEL/pax each call, so reuse the
  // roll the pilot is already looking at.
  if (expandedKey && document.getElementById('detailCell')) {
    if (expandedDetailData && expandedDetailKey === expandedKey) paintDetail();
    else loadDetail(expandedKey);
  }
}

let listScrollBeforeOpen = 0;

function toggleItinerary(key) {
  const opening = expandedKey !== key;
  // On desktop the briefing replaces the list in the right column: open it
  // at the top, and come back to the same place in the list on close.
  const scroller = document.getElementById('listScroll');
  if (opening && !expandedKey && scroller) listScrollBeforeOpen = scroller.scrollTop;
  expandedKey = opening ? key : null;
  expandedDetailData = null;
  expandedDetailKey = null;
  renderTable();
  // Beside the list on desktop, the map follows the flight being opened.
  refreshMapHighlight({ pan: opening && WIDE_MQ.matches });
  if (scroller && WIDE_MQ.matches) scroller.scrollTop = opening ? 0 : listScrollBeforeOpen;
}

async function loadDetail(key) {
  if (detailLoadingKey === key) return;
  detailLoadingKey = key;
  try {
    const data = await (await fetch(`/select?flights=${encodeURIComponent(key)}`)).json();
    if (expandedKey !== key) return;   // closed or switched while loading
    if (data.error) throw new Error(data.error);
    expandedDetailData = data;
    expandedDetailKey = key;
    await paintDetail();
  } catch (err) {
    if (expandedKey !== key) return;
    expandedDetailData = null;
    expandedDetailKey = null;
    const cell = document.getElementById('detailCell');
    if (cell) cell.innerHTML = `<div class="empty-state">COULD NOT LOAD FLIGHT DETAILS</div>`;
  } finally {
    if (detailLoadingKey === key) detailLoadingKey = null;
  }
}

async function paintDetail() {
  const [carriers, ownedAircraft] = await Promise.all([getCarriers(), getOwnedAircraft()]);
  const cell = document.getElementById('detailCell');
  if (!cell || !expandedDetailData || expandedDetailKey !== expandedKey) return;
  cell.innerHTML = buildDetailHtml(expandedDetailData, expandedKey, carriers, ownedAircraft);
  expandedDetailData.legs.forEach((leg, i) => initRouteMap(`map-detail-${expandedKey}-${i}`, leg));
}

// ---- Table <-> map linking (never re-renders the table) ----

function currentHighlightKey() { return hoverKey || pinnedKey || expandedKey; }

function hoverItinerary(key) {
  if (hoverKey === key) return;
  hoverKey = key;
  refreshMapHighlight();
}

function showItineraryOnMap(key, event) {
  pinnedKey = key;
  hoverKey = null;
  hoverSuppressedAt = event && event.screenX != null ? { x: event.screenX, y: event.screenY } : null;
  if (window.matchMedia('(max-width: 720px)').matches && !mapOpenOnMobile) toggleMapOnMobile();
  refreshMapHighlight({ pan: true });
  const panel = document.getElementById('mapPanel');
  // On desktop the map sits beside the list and its panel fits the screen:
  // scroll only if part of it is hidden, bringing all of it into view.
  if (panel && !WIDE_MQ.matches) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  else if (panel && !selectionMapFullyInView()) panel.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

function selectionMapFullyInView() {
  const el = document.getElementById('selectionMap');
  if (!el) return false;
  const r = el.getBoundingClientRect();
  const header = document.querySelector('.app-header');
  const top = header ? header.getBoundingClientRect().bottom : 0;
  return r.top >= top - 2 && r.bottom <= window.innerHeight + 2;
}

// ---- Desktop layout ----
const WIDE_MQ = window.matchMedia('(min-width: 1200px)');

// Keeps --sticky-top (header height plus a gap) current: desktop pages
// size themselves to the screen below the header.
function sizeSearchLayout() {
  const header = document.querySelector('.app-header');
  document.documentElement.style.setProperty('--sticky-top', ((header ? header.offsetHeight : 0) + 12) + 'px');
}

let layoutRaf = 0;
window.addEventListener('resize', () => {
  if (layoutRaf) return;
  layoutRaf = requestAnimationFrame(() => { layoutRaf = 0; sizeSearchLayout(); });
});

function syncRowHighlightClass() {
  const key = currentHighlightKey();
  document.querySelectorAll('#itinTableBody tr.itin-row').forEach(tr => {
    tr.classList.toggle('map-hl', tr.dataset.key === key);
  });
}

// ---- Base map: the app's own minimal background (land, borders, country
// names, major cities from Natural Earth, built by tools/basemap) instead
// of OpenStreetMap's detailed raster tiles. Shared by every map. ----
const BASEMAP_URL = VD_CONFIG.basemapUrl;
const BASEMAP_MAX_ZOOM = 8;
let basemapPromise = null;

function getBasemap() {
  if (!basemapPromise) {
    basemapPromise = fetch(BASEMAP_URL)
      .then(r => { if (!r.ok) throw new Error('basemap unavailable'); return r.json(); })
      .catch(err => { basemapPromise = null; throw err; });
  }
  return basemapPromise;
}

function escText(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function addBasemap(map) {
  map.getContainer().classList.add('vd-map');
  map.createPane('vdLand').style.zIndex = 200;
  const labelPane = map.createPane('vdLabels');
  labelPane.style.zIndex = 390;             // under routes/airports (400+)
  labelPane.style.pointerEvents = 'none';
  getBasemap().then(data => {
    if (!map.getContainer()._leaflet_id) return;   // map removed while loading
    L.geoJSON(data.countries, {
      pane: 'vdLand', renderer: L.canvas({ pane: 'vdLand' }), interactive: false,
      style: { fillColor: '#FBFBF8', fillOpacity: 1, color: '#C3CAD5', weight: 0.8 },
    }).addTo(map);
    // Label boxes are estimated from text length (px per character at the
    // CSS sizes below) rather than measured, so hidden labels need no DOM.
    const labels = [];
    const label = (lat, lon, minz, rank, className, html, box) => labels.push({
      latlng: L.latLng(lat, lon), minz, rank, box, shown: false,
      marker: L.marker([lat, lon], { pane: 'vdLabels', interactive: false, keyboard: false,
        icon: L.divIcon({ className, html, iconSize: null }) }),
    });
    data.countries.features.forEach(f => {
      const w = f.properties.name.length * 8.2;
      label(f.properties.label[1], f.properties.label[0], f.properties.minz, 0, 'vd-country-label',
        `<span>${escText(f.properties.name)}</span>`, [-w / 2, -7, w / 2, 7]);
    });
    data.cities.forEach(c => {
      const w = c.name.length * (c.cap ? 6.6 : 6.2);
      label(c.lat, c.lon, c.minz, c.cap ? 1 : 2, c.cap ? 'vd-city-label cap' : 'vd-city-label',
        `<i></i><span>${escText(c.name)}</span>`, [-4, -8, 6 + w, 6]);
    });
    // Countries first, then capitals, then other cities; within each, the
    // ones Natural Earth shows earliest are the more important ones.
    labels.sort((a, b) => a.rank - b.rank || a.minz - b.minz);
    const group = L.layerGroup().addTo(map);
    // Natural Earth's per-label minimum zoom keeps small countries and lesser
    // cities out until there's room; a label that would overlap one already
    // placed is skipped, so the map never shows text on top of text.
    const update = () => {
      const z = map.getZoom();
      const placed = [];
      labels.forEach(l => {
        let show = z >= l.minz;
        if (show) {
          const p = map.latLngToLayerPoint(l.latlng);
          const r = [p.x + l.box[0] - 3, p.y + l.box[1] - 2, p.x + l.box[2] + 3, p.y + l.box[3] + 2];
          show = !placed.some(q => r[0] < q[2] && q[0] < r[2] && r[1] < q[3] && q[1] < r[3]);
          if (show) placed.push(r);
        }
        if (show === l.shown) return;
        l.shown = show;
        if (show) group.addLayer(l.marker); else group.removeLayer(l.marker);
      });
    };
    map.on('zoomend', update);
    update();
    map.attributionControl.addAttribution('Base map: <a href="https://www.naturalearthdata.com/" target="_blank" rel="noopener">Natural Earth</a>');
  }).catch(() => {
    if (!map.getContainer()._leaflet_id) return;
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: BASEMAP_MAX_ZOOM, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
    }).addTo(map);
  });
}

// ---- Weather overlays on the route map: flight category per airport
// (current METAR) and SIGMET areas, via the cached /wx endpoints. Both are
// optional, off by default, and remembered per browser. ----
const WX_PREFS_KEY = 'vd_map_weather';
const WX_REFRESH_MS = 5 * 60 * 1000;
const FLIGHT_CAT_COLORS = { VFR: '#12B76A', MVFR: '#2E90FA', IFR: '#F04438', LIFR: '#D444F1' };
const SIGMET_COLORS = {
  TS: '#D92D20', TSGR: '#D92D20', CB: '#D92D20', TURB: '#F79009', ICE: '#1570EF',
  MTW: '#93370D', VA: '#6941C6', TC: '#912018', DS: '#A15C07', SS: '#A15C07', RDOACT: '#C11574',
};
const SIGMET_LEGEND = [['TS', 'THUNDERSTORMS'], ['TURB', 'TURBULENCE'], ['ICE', 'ICING'], ['MTW', 'MOUNTAIN WAVES'], ['VA', 'VOLCANIC ASH']];
const SIGMET_CHANGE = { NC: 'no change', WKN: 'weakening', INTSF: 'intensifying' };

let wxPrefs = loadWxPrefs();
const wxData = { cat: null, sigmet: null };
const wxState = { cat: 'idle', sigmet: 'idle' };   // idle | loading | ok | error
const wxLoadedAt = { cat: 0, sigmet: 0 };
let wxTimer = null;

function loadWxPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem(WX_PREFS_KEY) || '{}');
    return { cat: !!p.cat, sigmet: !!p.sigmet };
  } catch (e) { return { cat: false, sigmet: false }; }
}

function initWeather() {
  ['cat', 'sigmet'].forEach(k => {
    if (wxPrefs[k] && Date.now() - wxLoadedAt[k] > WX_REFRESH_MS) loadWeather(k);
  });
  syncWeatherTimer();
  renderWeatherChrome();
}

function setWeatherLayer(kind, on) {
  wxPrefs[kind] = on;
  try { localStorage.setItem(WX_PREFS_KEY, JSON.stringify(wxPrefs)); } catch (e) {}
  if (on && Date.now() - wxLoadedAt[kind] > WX_REFRESH_MS) loadWeather(kind);
  syncWeatherTimer();
  renderWeatherChrome();
  renderSelectionMap({ fit: false });
}

async function loadWeather(kind) {
  if (wxState[kind] === 'loading') return;
  wxState[kind] = 'loading';
  renderWeatherChrome();
  try {
    const resp = await fetch(kind === 'cat' ? '/wx/metars' : '/wx/sigmets');
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || 'unavailable');
    wxData[kind] = data;
    wxLoadedAt[kind] = Date.now();
    wxState[kind] = 'ok';
  } catch (err) {
    wxState[kind] = 'error';   // whatever was loaded before stays on screen, flagged
  }
  renderWeatherChrome();
  if (wxPrefs[kind] && selMap) renderSelectionMap({ fit: false });
}

function syncWeatherTimer() {
  const any = wxPrefs.cat || wxPrefs.sigmet;
  if (any && !wxTimer) {
    wxTimer = setInterval(() => {
      if (!document.getElementById('selectionMap')) { clearInterval(wxTimer); wxTimer = null; return; }
      ['cat', 'sigmet'].forEach(k => { if (wxPrefs[k]) loadWeather(k); });
    }, WX_REFRESH_MS);
  } else if (!any && wxTimer) {
    clearInterval(wxTimer);
    wxTimer = null;
  }
}

function zuluOf(iso) { return iso ? `${iso.slice(8, 10)}/${iso.slice(11, 13)}${iso.slice(14, 16)}Z` : '?'; }

function renderWeatherChrome() {
  const catToggle = document.getElementById('wxCatToggle');
  if (!catToggle) return;
  catToggle.checked = wxPrefs.cat;
  document.getElementById('wxSigmetToggle').checked = wxPrefs.sigmet;
  const names = { cat: 'METARs', sigmet: 'SIGMETs' };
  const parts = [];
  ['cat', 'sigmet'].forEach(k => {
    if (!wxPrefs[k]) return;
    const d = wxData[k], st = wxState[k];
    if (!d) { parts.push(st === 'error' ? `${names[k]} unavailable right now` : `${names[k]} loading…`); return; }
    const count = k === 'sigmet' ? `, ${plural(d.sigmets.length, 'active', 'active')}` : '';
    const old = st === 'error' || d.stale ? ' – outdated, refresh failed' : '';
    parts.push(`${names[k]} ${zuluOf(d.updated)}${count}${old}`);
  });
  document.getElementById('wxStatus').textContent = parts.join(' · ');

  const legend = document.getElementById('wxLegend');
  const items = [];
  if (wxPrefs.cat) {
    Object.entries(FLIGHT_CAT_COLORS).forEach(([c, col]) => items.push(`<span><i class="sw sw-dot" style="background:${col}"></i>${c}</span>`));
  }
  if (wxPrefs.sigmet) {
    SIGMET_LEGEND.forEach(([h, label]) => items.push(`<span><i class="sw sw-area" style="border-color:${SIGMET_COLORS[h]}; background:${SIGMET_COLORS[h]}22"></i>${label}</span>`));
  }
  legend.innerHTML = items.join('');
  legend.hidden = items.length === 0;
}

function metarLineHtml(icao) {
  if (!wxPrefs.cat || !wxData.cat) return '';
  const w = wxData.cat.stations[icao];
  if (!w || !w.raw) return '<span class="wx-raw">No current METAR</span>';
  const cat = FLIGHT_CAT_COLORS[w.cat] ? w.cat : null;
  return `${cat ? `<span class="wx-badge" style="background:${FLIGHT_CAT_COLORS[cat]}">${cat}</span> ` : ''}<span class="wx-raw">${escText(w.raw)}</span>`;
}

function flightLevel(ft) {
  if (ft == null || ft === '') return null;
  const n = Number(ft);
  if (!Number.isFinite(n)) return null;
  return n <= 0 ? 'SFC' : `FL${String(Math.round(n / 100)).padStart(3, '0')}`;
}

function sigmetSummary(sg) {
  const levels = [flightLevel(sg.base), flightLevel(sg.top)];
  const lv = levels[0] || levels[1] ? `${levels[0] || '?'}&ndash;${levels[1] || '?'}` : '';
  return { levels: lv, title: `${sg.hazard_name.toUpperCase()}${sg.qualifier ? ' (' + escText(sg.qualifier) + ')' : ''}` };
}

function drawSigmets() {
  if (!wxPrefs.sigmet || !wxData.sigmet) return;
  wxData.sigmet.sigmets.forEach(sg => {
    const color = SIGMET_COLORS[sg.hazard] || '#475467';
    const { levels, title } = sigmetSummary(sg);
    L.polygon(sg.coords, { color, weight: 1.5, dashArray: '6 4', fillColor: color, fillOpacity: 0.12 })
      .bindTooltip(`SIGMET &middot; ${title}${levels ? ' &middot; ' + levels : ''}<br>Click for details`, { sticky: true, className: 'sigmet-tip' })
      .on('click', (e) => { L.DomEvent.stopPropagation(e); openSigmetPopup(e.latlng, sg); })
      .addTo(selLayers.wx);
  });
}

function openSigmetPopup(latlng, sg) {
  const { levels, title } = sigmetSummary(sg);
  const move = sg.dir && sg.spd ? `Moving ${escText(sg.dir)} at ${escText(sg.spd)} kt` : (sg.dir === 'STNR' ? 'Stationary' : '');
  const change = SIGMET_CHANGE[sg.chng] ? `, ${SIGMET_CHANGE[sg.chng]}` : '';
  const html = `
    <div class="map-popup sigmet-popup">
      <div class="mp-title" style="color:${SIGMET_COLORS[sg.hazard] || '#475467'}">SIGMET &middot; ${title}</div>
      <div class="mp-name">${escText(sg.fir)}${sg.id ? ' &middot; ' + escText(sg.id) : ''}</div>
      <div class="mp-sub">Valid ${zuluOf(sg.from)} &ndash; ${zuluOf(sg.to)}${levels ? ' &middot; ' + levels : ''}${move ? '<br>' + move + change : ''}</div>
      ${sg.raw ? `<div class="sigmet-raw">${escText(sg.raw)}</div>` : ''}
    </div>`;
  L.popup({ autoPan: true, maxWidth: 320, className: 'map-popup-wrap' }).setLatLng(latlng).setContent(html).openOn(selMap);
}

// ---- Route map (Leaflet, own base map, canvas-rendered) ----

let selMap = null;
let selLayers = null;
let mapPoints = [];        // what "fit to results" frames
let mapAirportEntries = []; // every clickable airport currently drawn
let mapSegmentEntries = []; // every clickable route line currently drawn
let mapNeedsFit = false;   // a fit requested while the map was hidden

const MAP_COLORS = { dep: '#1D4ED8', dest: '#C4320A', airport: '#344054', route: '#2563EB', hl: '#6938EF' };
const AIRPORT_MARKER_STYLES = {
  overview: { radius: 4,   fillColor: '#475467', color: '#FFFFFF', weight: 1,   fillOpacity: 0.85 },
  endpoint: { radius: 5.5, fillColor: '#344054', color: '#FFFFFF', weight: 1.5, fillOpacity: 1 },
  via:      { radius: 5,   fillColor: '#FFFFFF', color: '#344054', weight: 2.5, fillOpacity: 1 },
  dep:      { radius: 9,   fillColor: '#1D4ED8', color: '#FFFFFF', weight: 2.5, fillOpacity: 1 },
  dest:     { radius: 9,   fillColor: '#C4320A', color: '#FFFFFF', weight: 2.5, fillOpacity: 1 },
};

function getNetwork() {
  if (networkData) return Promise.resolve(networkData);
  if (!networkPromise) {
    networkPromise = fetch('/network')
      .then(r => { if (!r.ok) throw new Error('network unavailable'); return r.json(); })
      .then(d => {
        const airportsByIcao = {};
        const airportMask = {};
        d.airports.forEach(a => { airportsByIcao[a.icao] = a; airportMask[a.icao] = 0; });
        const pairs = new Set();
        d.routes.forEach(([di, ai, mask]) => {
          const dep = d.airports[di].icao, arr = d.airports[ai].icao;
          pairs.add(`${dep}>${arr}`);
          airportMask[dep] |= mask;
          airportMask[arr] |= mask;
        });
        networkData = { ...d, airportsByIcao, airportMask, pairs };
        return networkData;
      })
      .catch(err => { networkPromise = null; throw err; });
  }
  return networkPromise;
}

function carrierBit(code) {
  const i = networkData ? networkData.carriers.indexOf(code) : -1;
  return i < 0 ? 0 : (1 << i);
}

function showMapOverlay(html) {
  const el = document.getElementById('mapOverlay');
  if (!el) return;
  el.hidden = !html;
  el.innerHTML = html || '';
}

function ensureSelectionMap() {
  const el = document.getElementById('selectionMap');
  if (!el) return null;
  if (typeof L === 'undefined') { showMapOverlay('MAP UNAVAILABLE &mdash; THE FIELDS AND TABLE STILL WORK'); return null; }
  if (selMap && selMap.getContainer() === el) return selMap;
  if (selMap) selMap.remove();
  // keyboard:false keeps the map out of the tab order - the fields and
  // table are the complete keyboard path; the map is a visual aid.
  selMap = L.map(el, {
    renderer: L.canvas({ padding: 0.5, tolerance: 5 }),
    // Mouse-wheel zoom where there's a mouse/trackpad; touch keeps pinch.
    zoomControl: true, scrollWheelZoom: window.matchMedia('(hover: hover) and (pointer: fine)').matches,
    wheelPxPerZoomLevel: 120, keyboard: false,
    minZoom: 3, maxZoom: BASEMAP_MAX_ZOOM, worldCopyJump: true,
  });
  addBasemap(selMap);
  selLayers = {
    wx: L.layerGroup().addTo(selMap),
    routes: L.layerGroup().addTo(selMap),
    airports: L.layerGroup().addTo(selMap),
    highlight: L.layerGroup().addTo(selMap),
  };
  selMap.setView([47, 8], 4);
  // A SIGMET's hover tooltip would otherwise sit right under its own popup.
  selMap.on('popupopen', () => el.classList.add('popup-open'));
  selMap.on('popupclose', () => el.classList.remove('popup-open'));
  el.classList.toggle('pick-direct', mapPickMode !== 'ask');
  // On desktop the map's height follows the space left on screen (the
  // greeting arriving, chips wrapping); Leaflet must be told when it changes.
  if (window.ResizeObserver) {
    const map = selMap;
    new ResizeObserver(() => { if (map === selMap) map.invalidateSize(); }).observe(el);
  }
  return selMap;
}

// Segments and airports for the current results, after every filter
// except the route focus (the focus highlights one segment among them).
function buildMapModel() {
  const segments = new Map();
  const airportsInfo = new Map();
  const note = (icao, role) => {
    let info = airportsInfo.get(icao);
    if (!info) { info = { endpoint: false, via: false, count: 0 }; airportsInfo.set(icao, info); }
    info[role] = true;
    info.count++;
  };
  filteredItineraries('route').forEach(it => {
    const far = it.via_icao || it.arrival_icao;
    const key = routeKeyFor(it.departure_icao, far);
    let seg = segments.get(key);
    if (!seg) { seg = { key, a: it.departure_icao, b: far, oneLeg: 0, multi: 0 }; segments.set(key, seg); }
    note(it.departure_icao, 'endpoint');
    if (it.trip_type === 'RT') { seg.multi++; note(it.via_icao, 'via'); }
    else { seg.oneLeg++; note(it.arrival_icao, 'endpoint'); }
  });
  return { segments, airportsInfo };
}

function plural(n, one, many) { return `${fmtNum(n)} ${n === 1 ? one : many}`; }

function renderSelectionMap({ fit }) {
  const map = ensureSelectionMap();
  if (!map) return;
  selLayers.wx.clearLayers();
  selLayers.routes.clearLayers();
  selLayers.airports.clearLayers();
  selLayers.highlight.clearLayers();
  mapPoints = [];
  mapAirportEntries = [];
  mapSegmentEntries = [];
  if (!networkData) {
    showMapOverlay(networkPromise ? 'LOADING MAP&hellip;' : 'MAP DATA UNAVAILABLE &mdash; THE FIELDS AND TABLE STILL WORK');
    return;
  }
  const s = searchState;
  const ab = networkData.airportsByIcao;
  // Canvas draw order is add order: SIGMET areas, routes, category rings,
  // airport markers, highlight - so airports and routes stay on top and
  // keep receiving clicks inside a SIGMET area.
  drawSigmets();
  const airportQueue = [];

  if (searchStatus === 'idle') {
    const mask = s.airline ? carrierBit(s.airline) : 0;
    networkData.airports.forEach(a => {
      if (mask && !(networkData.airportMask[a.icao] & mask)) return;
      airportQueue.push([a, 'overview', null]);
    });
    drawAirports(airportQueue);
    showMapOverlay(null);
  } else {
    const model = searchStatus === 'ready' ? buildMapModel() : { segments: new Map(), airportsInfo: new Map() };
    model.segments.forEach(seg => {
      const A = ab[seg.a], B = ab[seg.b];
      if (!A || !B) return;
      const focused = s.route === seg.key;
      const n = seg.oneLeg + seg.multi;
      const parts = [seg.oneLeg ? plural(seg.oneLeg, 'one-leg', 'one-leg') : '', seg.multi ? plural(seg.multi, 'multi-leg', 'multi-leg') : ''].filter(Boolean).join(', ');
      L.polyline([[A.lat, A.lon], [B.lat, B.lon]], {
        color: focused ? MAP_COLORS.dep : MAP_COLORS.route,
        weight: focused ? 4.5 : 2,
        opacity: s.route && !focused ? 0.18 : (focused ? 1 : 0.6),
      }).bindTooltip(`${seg.a} &ndash; ${seg.b} &middot; ${plural(n, 'itinerary', 'itineraries')} (${parts})<br>${focused ? 'Click to show every route again' : 'Click to list only these in the table'}`, { sticky: true })
        .on('click', (e) => { L.DomEvent.stopPropagation(e); onMapRouteClick(e, seg); })
        .addTo(selLayers.routes);
      mapSegmentEntries.push({ seg, n, A, B });
    });
    model.airportsInfo.forEach((info, icao) => {
      if (icao === s.origin || icao === s.destination || !ab[icao]) return;
      airportQueue.push([ab[icao], info.endpoint ? 'endpoint' : 'via', info]);
    });
    if (s.origin && ab[s.origin]) airportQueue.push([ab[s.origin], 'dep', model.airportsInfo.get(s.origin)]);
    if (s.destination && ab[s.destination]) airportQueue.push([ab[s.destination], 'dest', model.airportsInfo.get(s.destination)]);
    drawAirports(airportQueue);

    if (searchStatus === 'loading') showMapOverlay('<span class="spinner" aria-hidden="true"></span>SEARCHING&hellip;');
    else if (searchStatus === 'error') showMapOverlay('SEARCH ERROR &mdash; SEE BELOW');
    else if (model.segments.size === 0) showMapOverlay('NO MATCHING ROUTES &mdash; SEE THE SUGGESTIONS BELOW');
    else showMapOverlay(null);
  }
  if (fit || mapNeedsFit) fitSelectionMap();
  refreshMapHighlight();
}

function drawAirports(queue) {
  if (wxPrefs.cat && wxData.cat) {
    queue.forEach(([a, kind]) => {
      const w = wxData.cat.stations[a.icao];
      if (!w || !FLIGHT_CAT_COLORS[w.cat]) return;
      L.circleMarker([a.lat, a.lon], {
        radius: AIRPORT_MARKER_STYLES[kind].radius + 4, stroke: false,
        fillColor: FLIGHT_CAT_COLORS[w.cat], fillOpacity: 0.95, interactive: false,
      }).addTo(selLayers.airports);
    });
  }
  queue.forEach(([a, kind, info]) => addAirportMarker(a, kind, info));
}

function addAirportMarker(a, kind, info) {
  mapPoints.push([a.lat, a.lon]);
  const marker = L.circleMarker([a.lat, a.lon], AIRPORT_MARKER_STYLES[kind]).addTo(selLayers.airports);
  const wx = metarLineHtml(a.icao);
  if (kind === 'dep' || kind === 'dest') {
    marker.bindTooltip(`${a.icao} &middot; ${kind === 'dep' ? 'DEPARTURE' : 'DESTINATION'}`,
      { permanent: true, direction: 'top', offset: [0, -9], className: `map-label map-label-${kind}` });
  } else {
    const count = info ? ` &middot; ${plural(info.count, 'itinerary', 'itineraries')}` : '';
    const via = kind === 'via' ? '<br>Multi-leg turnaround (via) only' : '';
    marker.bindTooltip(`<strong>${a.icao}</strong> &middot; ${a.city}${count}${via}${wx ? '<br>' + wx : ''}`,
      { direction: 'top', offset: [0, -6], className: wx ? 'wx-tip' : '' });
  }
  mapAirportEntries.push({ a, info });
  marker.on('click', (e) => { L.DomEvent.stopPropagation(e); onMapAirportMarkerClick(e, a, info); });
}

// Close airports (London, Milan, Paris...) overlap at low zoom, so a click
// covering more than one opens a short "which airport?" list instead of
// silently picking whichever marker happens to be drawn on top.
function onMapAirportMarkerClick(e, a, info) {
  const pt = e.containerPoint;
  const nearby = mapAirportEntries
    .map(x => ({ ...x, d: selMap.latLngToContainerPoint([x.a.lat, x.a.lon]).distanceTo(pt) }))
    .filter(x => x.d <= 12)
    .sort((x, y) => x.d - y.d);
  const unique = nearby.filter((x, i) => nearby.findIndex(y => y.a.icao === x.a.icao) === i);
  if (unique.length <= 1) { onMapAirportClick(a, info); return; }
  const html = `
    <div class="map-popup">
      <div class="mp-title">WHICH AIRPORT?</div>
      <div class="mp-sub">${unique.length} airports overlap here &mdash; zoom in, or choose one:</div>
      <div class="mp-actions">${unique.map(x => `<button class="ghost" type="button" onclick="mapPickFromList('${x.a.icao}')"><strong>${x.a.icao}</strong>&nbsp;${x.a.name}</button>`).join('')}</div>
    </div>`;
  L.popup({ autoPan: true, maxWidth: 300, className: 'map-popup-wrap' }).setLatLng(e.latlng).setContent(html).openOn(selMap);
}

// Routes fanning out of one airport run close together, so a click within
// reach of several lines asks which one rather than guessing.
function onMapRouteClick(e, seg) {
  const pt = e.containerPoint;
  const near = mapSegmentEntries
    .map(x => ({ ...x, d: L.LineUtil.pointToSegmentDistance(pt,
        selMap.latLngToContainerPoint([x.A.lat, x.A.lon]), selMap.latLngToContainerPoint([x.B.lat, x.B.lon])) }))
    .filter(x => x.d <= 7)
    .sort((x, y) => x.d - y.d);
  if (near.length <= 1) { toggleRouteFocus(seg.key); return; }
  const shown = near.slice(0, 8);
  const more = near.length - shown.length;
  const html = `
    <div class="map-popup">
      <div class="mp-title">WHICH ROUTE?</div>
      <div class="mp-sub">${near.length} routes pass here &mdash; choose one to list its itineraries${more ? `, or zoom in to separate the other ${more}` : ''}:</div>
      <div class="mp-actions">${shown.map(x => `<button class="ghost" type="button" onclick="selMap.closePopup(); toggleRouteFocus('${x.seg.key}')"><strong>${x.seg.a} &ndash; ${x.seg.b}</strong>&nbsp;&middot; ${plural(x.n, 'itinerary', 'itineraries')}</button>`).join('')}</div>
    </div>`;
  L.popup({ autoPan: true, maxWidth: 300, className: 'map-popup-wrap' }).setLatLng(e.latlng).setContent(html).openOn(selMap);
}

function mapPickFromList(icao) {
  const entry = mapAirportEntries.find(x => x.a.icao === icao);
  if (!entry) return;
  selMap.closePopup();
  onMapAirportClick(entry.a, entry.info);
}

function onMapAirportClick(a, info) {
  const s = searchState;
  const selectedRole = s.origin === a.icao ? 'origin' : (s.destination === a.icao ? 'destination' : null);
  if (mapPickMode !== 'ask' && !selectedRole) { mapSetAirport(mapPickMode, a.icao); return; }
  openAirportPopup(a, selectedRole, info);
}

function openAirportPopup(a, selectedRole, info) {
  const s = searchState;
  const actions = [];
  let sub;
  if (selectedRole) {
    const roleName = selectedRole === 'origin' ? 'DEPARTURE' : 'DESTINATION';
    const other = selectedRole === 'origin' ? 'destination' : 'origin';
    sub = `Selected as ${roleName.toLowerCase()}.`;
    actions.push(`<button class="ghost" type="button" onclick="mapClearAirport('${selectedRole}')">REMOVE AS ${roleName}</button>`);
    if (s.origin && s.destination) actions.push(`<button class="ghost" type="button" onclick="swapAirports()">&#8644; SWAP DEPARTURE AND DESTINATION</button>`);
    else actions.push(`<button class="ghost" type="button" onclick="mapSetAirport('${other}','${a.icao}')">USE AS ${other === 'origin' ? 'DEPARTURE' : 'DESTINATION'} INSTEAD</button>`);
  } else {
    const n = info ? info.count : null;
    sub = n != null ? `${plural(n, 'matching itinerary', 'matching itineraries')} with the current search`
                    : `${plural(networkDestinationCount(a.icao), 'destination', 'destinations')} &middot; ${plural(networkOriginCount(a.icao), 'origin', 'origins')} in the network`;
    const depSuffix = n != null && s.destination && !s.origin ? ` (${fmtNum(n)})` : '';
    const destSuffix = n != null && s.origin && !s.destination ? ` (${fmtNum(n)})` : '';
    actions.push(`<button class="action small" type="button" onclick="mapSetAirport('origin','${a.icao}')">SET AS DEPARTURE${depSuffix}</button>`);
    actions.push(`<button class="action small" type="button" onclick="mapSetAirport('destination','${a.icao}')">SET AS DESTINATION${destSuffix}</button>`);
  }
  const html = `
    <div class="map-popup">
      <div class="mp-title">${a.icao} / ${a.iata}</div>
      <div class="mp-name">${a.name}, ${a.city} (${a.country})</div>
      ${metarLineHtml(a.icao) ? `<div class="mp-wx">${metarLineHtml(a.icao)}</div>` : ''}
      <div class="mp-sub">${sub}</div>
      <div class="mp-actions">${actions.join('')}</div>
    </div>`;
  L.popup({ autoPan: true, maxWidth: 280, className: 'map-popup-wrap' })
    .setLatLng([a.lat, a.lon]).setContent(html).openOn(selMap);
}

function mapClearAirport(role) {
  if (selMap) selMap.closePopup();
  setAirport(role, '');
}

function fitSelectionMap() {
  if (!selMap) return;
  const el = selMap.getContainer();
  if (!el.offsetWidth) { mapNeedsFit = true; return; }
  mapNeedsFit = false;
  selMap.invalidateSize();
  // No animation: Leaflet drops a view change requested while a zoom
  // animation is still running, so an earlier fit could win over this one.
  if (mapPoints.length === 1) selMap.setView(mapPoints[0], 6, { animate: false });
  else if (mapPoints.length > 1) selMap.fitBounds(L.latLngBounds(mapPoints), { padding: [30, 30], maxZoom: 7, animate: false });
}

// Draws the highlighted itinerary (hovered, focused, pinned or expanded
// row) on top of everything, with its intermediate stop drawn distinctly
// from its origin and final destination.
function refreshMapHighlight(opts) {
  syncRowHighlightClass();
  if (!selMap || !selLayers || !networkData) return;
  selLayers.highlight.clearLayers();
  const key = currentHighlightKey();
  const itin = key && currentItineraries.find(i => itinKey(i) === key);
  if (!itin) return;
  const ab = networkData.airportsByIcao;
  const A = ab[itin.departure_icao];
  const B = ab[itin.via_icao || itin.arrival_icao];
  if (!A || !B) return;
  const line = L.polyline([[A.lat, A.lon], [B.lat, B.lon]], { color: MAP_COLORS.hl, weight: 5, opacity: 0.95, interactive: false }).addTo(selLayers.highlight);
  L.circleMarker([A.lat, A.lon], { radius: 7, fillColor: MAP_COLORS.hl, color: '#FFFFFF', weight: 2, fillOpacity: 1, interactive: false }).addTo(selLayers.highlight);
  const path = itin.trip_type === 'RT'
    ? `${itin.departure_icao} &rarr; <em>${itin.via_icao} (via)</em> &rarr; ${itin.arrival_icao}`
    : `${itin.departure_icao} &rarr; ${itin.arrival_icao}`;
  if (itin.trip_type === 'RT') {
    L.circleMarker([B.lat, B.lon], { radius: 7, fillColor: '#FFFFFF', color: MAP_COLORS.hl, weight: 3, fillOpacity: 1, interactive: false }).addTo(selLayers.highlight);
  } else {
    L.circleMarker([B.lat, B.lon], { radius: 7, fillColor: MAP_COLORS.hl, color: '#FFFFFF', weight: 2, fillOpacity: 1, interactive: false }).addTo(selLayers.highlight);
  }
  line.bindTooltip(`${itin.flight_numbers.join(' / ')} &middot; ${path}`, { permanent: true, direction: 'center', className: 'map-label map-label-hl' }).openTooltip();
  if (opts && opts.pan) selMap.fitBounds(L.latLngBounds([[A.lat, A.lon], [B.lat, B.lon]]), { padding: [50, 50], maxZoom: 7 });
}

function fleetOptionsFor(leg, carriers, ownedAircraft) {
  const carrier = carriers.find(c => c.icao === leg.carrier);
  if (!carrier) return [];
  const owned = carrier.fleet.filter(ac => ownedAircraft.has(ac.type));
  return owned.length ? owned : carrier.fleet;
}

function legScheduleTable(leg) {
  return `
        <table class="kv" style="margin-bottom:10px;">
          <tr><td class="k"></td><td class="v h">SCHEDULED</td><td class="v h">EXPECTED</td></tr>
          <tr><td class="k">OBT (OFF-BLOCK)</td><td class="v">${leg.sobt || 'N/A'}</td><td class="v">${leg.eobt || 'N/A'}</td></tr>
          <tr><td class="k">TOT (TAKE-OFF)</td><td class="v">${leg.stot || 'N/A'}</td><td class="v">${leg.etot || 'N/A'}</td></tr>
          <tr><td class="k">LDT (LANDING)</td><td class="v">${leg.sldt || 'N/A'}</td><td class="v">${leg.eldt || 'N/A'}</td></tr>
          <tr><td class="k">IBT (IN-BLOCK)</td><td class="v">${leg.sibt || 'N/A'}</td><td class="v">${leg.eibt || 'N/A'}</td></tr>
          <tr><td class="k">EET</td><td class="v" colspan="2">${leg.eet_minutes != null ? fmtMinutes(leg.eet_minutes) : 'N/A'}</td></tr>
          ${leg.turnaround_minutes !== undefined ? `<tr><td class="k">TURNAROUND (FROM PREV LEG)</td><td class="v" colspan="2">${leg.turnaround_minutes != null ? fmtMinutes(leg.turnaround_minutes) : 'N/A'}</td></tr>` : ''}
          <tr><td class="k">COST INDEX</td><td class="v" colspan="2">${fmtNum(leg.cost_index)}</td></tr>
        </table>`;
}

function legLoadTable(leg, legIndex, fleetOptions) {
  const aircraftCell = (fleetOptions && fleetOptions.length > 1)
    ? `<select class="aircraft-reassign-select" data-leg-index="${legIndex}" onchange="reassignAircraft(this)">
         ${fleetOptions.map(ac => `<option value="${escAttr(ac.type)}" ${ac.type === leg.aircraft_type ? 'selected' : ''}>${escAttr(ac.type)} (${ac.seats} seats)</option>`).join('')}
       </select>`
    : `${escAttr(leg.aircraft_type || 'N/A')}`;
  return `
        <table class="kv" style="margin-bottom:10px;">
          <tr><td class="k">AIRCRAFT</td><td class="v">${aircraftCell}</td></tr>
          <tr><td class="k">EXPECTED PAX</td><td class="v">${fmtNum(leg.pax_count)} / ${fmtNum(leg.seat_capacity)}</td></tr>
          <tr><td class="k">EXPECTED LOAD FACTOR</td><td class="v">${(leg.load_factor * 100).toFixed(0)}%</td></tr>
          <tr><td class="k">EXPECTED CARGO/BAGS</td><td class="v">${fmtNum(leg.cargo_weight_kg)} KG</td></tr>
        </table>`;
}

// One aircraft flies every leg of the trip, so changing it changes them all
// (each leg keeps its own load factor).
async function reassignAircraft(selectEl) {
  const aircraftType = selectEl.value;
  selectEl.disabled = true;
  try {
    const results = await Promise.all(expandedDetailData.legs.map(async leg => {
      const params = new URLSearchParams({
        flight_number: leg.flight_number, aircraft_type: aircraftType, load_factor: leg.load_factor,
      });
      const result = await (await fetch(`/select/reassign-aircraft?${params}`)).json();
      if (result.error) throw new Error(result.error);
      return result;
    }));
    expandedDetailData.legs.forEach((leg, i) => Object.assign(leg, results[i]));
    const cell = document.getElementById('detailCell');
    const [carriers, ownedAircraft] = await Promise.all([getCarriers(), getOwnedAircraft()]);
    cell.innerHTML = buildDetailHtml(expandedDetailData, expandedKey, carriers, ownedAircraft);
    expandedDetailData.legs.forEach((l, i) => initRouteMap(`map-detail-${expandedKey}-${i}`, l));
  } catch (err) {
    alert('Could not change the aircraft. Check the connection and retry');
    selectEl.disabled = false;
  }
}

function legWeatherBlock(leg) {
  return `
        <div class="weather-block">
          <div class="weather-row"><span class="weather-label">METAR ${leg.departure_info.icao}</span><span class="weather-text">${escText(leg.weather.departure.metar || 'UNAVAILABLE')}</span></div>
          <div class="weather-row"><span class="weather-label">TAF ${leg.departure_info.icao}</span><span class="weather-text">${escText(leg.weather.departure.taf || 'UNAVAILABLE')}</span></div>
          <div class="weather-row"><span class="weather-label">METAR ${leg.arrival_info.icao}</span><span class="weather-text">${escText(leg.weather.arrival.metar || 'UNAVAILABLE')}</span></div>
          <div class="weather-row"><span class="weather-label">TAF ${leg.arrival_info.icao}</span><span class="weather-text">${escText(leg.weather.arrival.taf || 'UNAVAILABLE')}</span></div>
        </div>`;
}

// Expected departure delay of a leg, in minutes: its own rolled delay
// (sampled length; older flights stored only a range, whose top was used)
// or, once the previous leg's PIREP is in, the larger of that and the
// knock-on delay from the late inbound aircraft (see applyReactionaryDelay).
function legOwnDelayMinutes(leg) {
  const rolled = !leg.delay ? 0 : Number.isFinite(leg.delay.minutes) ? leg.delay.minutes : Math.max(...leg.delay.duration_range_minutes);
  return rolled + (leg.aircraft_change ? leg.aircraft_change.minutes : 0);
}
function legExpectedDelayMinutes(leg) {
  return Number.isFinite(leg.expected_delay_minutes) ? leg.expected_delay_minutes : legOwnDelayMinutes(leg);
}
// ---- Operational constraints: ATFM slot, curfews, crew duty (see ops.py) ----
function slotWindowText(atfm) {
  const [lo, hi] = atfm.window || [-5, 10];
  return `${shiftZulu(atfm.ctot, lo)} and ${shiftZulu(atfm.ctot, hi)}`;
}
// A slot only holds while the expected take-off stays inside its window;
// a later take-off means a new, later slot (Eurocontrol re-slots the flight).
function revalidateSlot(leg) {
  if (!leg.atfm || !leg.etot) return;
  const ctot = zuluMinutes(leg.atfm.ctot), etot = zuluMinutes(leg.etot);
  if (ctot == null || etot == null) return;
  if (clockDiff(ctot, etot) > (leg.atfm.window || [-5, 10])[1]) {
    leg.atfm = { ...leg.atfm, ctot: leg.etot.slice(0, 6), revised: true };
  }
}
// Is a Zulu minute-of-day inside the closed period [until, opens)?
function curfewState(t, c) {
  const until = zuluMinutes(c.until_utc), opens = zuluMinutes(c.opens_utc);
  if (t == null || until == null || opens == null) return null;
  const sinceClose = ((t - until) % 1440 + 1440) % 1440, closedSpan = ((opens - until) % 1440 + 1440) % 1440;
  if (sinceClose > 0 && sinceClose < closedSpan) return { closed: true, late: sinceClose };
  return { closed: false, margin: ((until - t) % 1440 + 1440) % 1440 };
}
function legCurfewChecks(leg) {
  return (leg.curfews || []).map(c => {
    const timeText = c.check === 'landing' ? leg.eldt : c.check === 'offblock' ? leg.eobt : leg.etot;
    const what = c.check === 'landing' ? 'landing' : c.check === 'offblock' ? 'off-block' : 'take-off';
    const st = curfewState(zuluMinutes(timeText), c);
    if (!st) return null;
    const limit = `${escText(c.name)} ${what} limit ${c.until_local} local (${c.until_utc})`;
    const outcome = c.kind === 'arrival' ? 'the flight would have to divert' : 'the flight would be cancelled';
    if (st.closed) return { icao: c.icao, level: 'nogo', text: `Expected ${what} ${timeText} is ${st.late} min after the ${limit}: ${outcome}. ${escText(c.note)}` };
    if (st.margin < 30) return { icao: c.icao, level: 'caution', text: `${limit}: only ${st.margin} min of margin on the expected ${what}` };
    return { icao: c.icao, level: 'ok', text: `${limit}: ${fmtMinutes(st.margin)} margin` };
  }).filter(Boolean);
}
// Flight duty period: report time to the last on-blocks (actual when the
// last PIREP is in, expected before), against EASA ORO.FTL.205.
function dutyFigures(legs, legStates) {
  const duty = legs[0] && legs[0].duty;
  if (!duty) return null;
  const last = legs.length - 1;
  const lastIn = (legStates && legStates[last] && legStates[last].pirep && legStates[last].pirep.abit) || legs[last].eibt;
  const start = zuluMinutes(legs[0].sobt), end = zuluMinutes(lastIn);
  if (start == null || end == null) return null;
  const fdp = duty.report_before_sobt + (((end - start) % 1440) + 1440) % 1440;
  const over = fdp - duty.max_fdp_minutes;
  return { duty, fdp, over, level: over > duty.discretion_minutes ? 'nogo' : over > 0 ? 'caution' : 'ok' };
}
function dutyCheck(legs, legStates) {
  const f = dutyFigures(legs, legStates);
  if (!f) return null;
  const duty = f.duty, fdp = f.fdp;
  const head = `Crew duty: report ${duty.report_local} local (${duty.report_utc}), maximum FDP ${fmtMinutes(duty.max_fdp_minutes)} for ${duty.sectors} sector${duty.sectors > 1 ? 's' : ''}`;
  const over = fdp - duty.max_fdp_minutes;
  if (over > duty.discretion_minutes) return { level: 'nogo', text: `${head}. Expected FDP ${fmtMinutes(fdp)} is beyond even the 2-hour commander's discretion: the crew runs out of hours` };
  if (over > 0) return { level: 'caution', text: `${head}. Expected FDP ${fmtMinutes(fdp)}: ${fmtMinutes(over)} into commander's discretion (up to 2 hours)` };
  return { level: 'ok', text: `${head}. Expected FDP ${fmtMinutes(fdp)}, ${fmtMinutes(-over)} to spare` };
}
function opsConstraintsHtml(legs, i, legStates) {
  const checks = legCurfewChecks(legs[i]);
  const duty = dutyCheck(legs, legStates);
  if (duty) checks.push(duty);
  if (!checks.length) return '';
  return `<ul class="tech-checks">${checks.map(c => `<li class="${c.level}">${c.text}</li>`).join('')}</ul>`;
}

// ---- Briefing rows: a fixed tag column and the text, one line each ----
// Shared by the trip preview in flight search and the active flight briefing.
function briefRow(tag, text, tone = '') {
  return `<div class="brief-row${tone ? ' ' + tone : ''}"><span class="brief-tag">${tag}</span><span class="brief-text">${text}</span></div>`;
}
function legDelayRows(leg) {
  const own = legOwnDelayMinutes(leg), r = leg.reactionary ? leg.reactionary.minutes : 0;
  const rows = [];
  if (leg.reactionary) rows.push(briefRow(`DELAY ${r} MIN`, leg.reactionary.expected ? 'Late inbound aircraft expected from the previous sector' : 'Late inbound aircraft from the previous sector', 'warn'));
  if (leg.delay) rows.push(briefRow(`DELAY ${Number.isFinite(leg.delay.minutes) ? leg.delay.minutes : Math.max(...leg.delay.duration_range_minutes)} MIN`, `${escText(leg.delay.plain || leg.delay.description)} <span class="brief-note">IATA ${escText(leg.delay.iata_code)}</span>`, 'warn'));
  if (leg.aircraft_change) rows.push(briefRow(`DELAY ${leg.aircraft_change.minutes} MIN`, 'Aircraft change <span class="brief-note">IATA 46</span>', 'warn'));
  if (leg.reactionary && own) rows.push(briefRow('TOTAL', `${legExpectedDelayMinutes(leg)} min expected: the other delays are absorbed during the late turnaround`));
  if (leg.atfm) rows.push(briefRow(`SLOT ${escText(leg.atfm.ctot)}`, `ATC slot (${escText(leg.atfm.reason.toLowerCase())}, ${escText(leg.atfm.where)}): take off between ${slotWindowText(leg.atfm)}${leg.atfm.revised ? '. New slot: the first one was missed' : ''}`, 'warn'));
  if (!rows.length) rows.push(briefRow('ON TIME', 'No delay expected', 'ok'));
  return rows;
}

// ---- Technical status (MEL/CDL, see techlog.py) ----
function legMels(leg) { return leg.mels || (leg.mel ? [leg.mel] : []); }
function legCdl(leg) { return leg.cdl || []; }
// Plain wording first (name/plain/steps in the data); the official MEL
// text only appears folded away. Older saved flights lack the plain
// fields, so everything falls back to the official text minus (M)/(O).
function stripMO(text) { return String(text || '').replace(/^\s*(\((M|O)\)\s*)+/, ''); }
// Which unit of a system: "2" reads "no. 2", "Left" reads "left", ESS stays ESS
function sideLabel(side) {
  const s = String(side || '');
  if (/^\d+$/.test(s)) return `no. ${s}`;
  return s && s !== s.toUpperCase() ? s.charAt(0).toLowerCase() + s.slice(1) : s;
}
function techTitle(m) {
  return escText(m.name || m.system || m.part || '') + (m.chosen_component ? `, ${escText(sideLabel(m.chosen_component))}` : '');
}
function techRef(m) { return escText(m.ata || m.mmel_ref || ''); }
function legTechRows(leg) {
  const rows = legMels(leg).map(m => briefRow(`MEL ${techRef(m)}`,
    `<b>${techTitle(m)}</b>: ${escText(m.plain || m.description)}`, 'warn'))
    .concat(legCdl(leg).map(c => briefRow(`CDL ${techRef(c)}`, `<b>${techTitle(c)}</b>: ${escText(c.plain || c.description)}`, 'warn')));
  return rows.length ? rows : [briefRow('TECH LOG', 'No deferred defects or missing panels', 'ok')];
}
function techChecksHtml(leg) {
  const checks = (leg.tech && leg.tech.checks) || [];
  return checks.length ? `<ul class="tech-checks">${checks.map(c => `<li class="${c.level}">${escText(c.text)}</li>`).join('')}</ul>` : '';
}
function techAppliedText(leg) {
  const t = leg.tech; if (!t) return '';
  const parts = [];
  if (t.max_fl) parts.push(`cruise no higher than FL${t.max_fl}`);
  if (t.extra_fuel_min) parts.push(`${t.extra_fuel_min} min of extra fuel`);
  if (legMels(leg).length || legCdl(leg).length) parts.push('every defect in full in the dispatch remarks');
  return parts.length ? `Included in your SimBrief plan: <strong>${parts.join(' &middot; ')}</strong>` : '';
}
// Trip preview: delays and defects as rows, then the dispatch checks
function legDelayMelBlock(leg) {
  const legacy = !leg.tech && legMels(leg)[0] && legMels(leg)[0].dispatch_consequence ? `<p class="tech-applied">${escText(legMels(leg)[0].dispatch_consequence)}</p>` : '';
  const applied = techAppliedText(leg);
  return `<div class="brief-list">${legDelayRows(leg).join('')}${legTechRows(leg).join('')}</div>
    ${techChecksHtml(leg)}${legacy}${applied ? `<p class="tech-applied">${applied}</p>` : ''}`;
}
// The whole MEL/CDL text for SimBrief's dispatcher remarks: one block per
// item with the official wording, the crew actions and what dispatch did.
function simbriefRemarks(leg) {
  const up = t => String(t || '').toUpperCase().replace(/°/g, ' ').replace(/[–—]/g, '-').replace(/\s+/g, ' ').trim();
  const lines = [];
  legMels(leg).forEach(m => {
    const due = m.interval_days ? ` CAT ${m.mel_category || '-'} ${m.interval_days === 1 ? 'FIX TODAY' : `FIX WITHIN ${m.interval_days} DAYS`}` : '';
    lines.push(`MEL ${up(m.ata || m.mmel_ref)}${due}: ${up(m.description)}`);
    const official = (m.procedures || []).map(stripMO).filter(Boolean);
    if (official.length) lines.push(`MEL PROVISOS: ${official.map(up).join('; ')}`);
    if ((m.steps || []).length) lines.push(`CREW ACTIONS: ${m.steps.map(up).join('; ')}`);
    if (m.dispatch_consequence) lines.push(`DISPATCH: ${up(m.dispatch_consequence)}`);
  });
  legCdl(leg).forEach(c => {
    lines.push(`CDL ${up(c.ata)}: ${up(c.description || c.part)}`);
    if ((c.steps || []).length) lines.push(`CREW ACTIONS: ${c.steps.map(up).join('; ')}`);
  });
  const t = leg.tech || {};
  const limits = [t.max_fl ? `MAX FL${t.max_fl}` : '', t.extra_fuel_min ? `EXTRA FUEL ${t.extra_fuel_min} MIN` : '',
    t.weight_penalty_kg ? `MTOW AND MLW -${t.weight_penalty_kg} KG` : ''].filter(Boolean);
  if (limits.length) lines.push(`DISPATCH LIMITS: ${limits.join(' / ')}`);
  return lines.join('\n');
}

function itineraryNogo(legs) {
  return legs.flatMap((leg, i) => ((leg.tech && leg.tech.checks) || []).filter(c => c.level === 'nogo').map(c => ({ leg: i, ...c })));
}
function toggleTechStep(i, key, input) {
  const flight = loadActiveFlight(); if (!flight) return;
  const st = flight.leg_state[i];
  st.tech_done = st.tech_done || {};
  if (input.checked) st.tech_done[key] = true; else delete st.tech_done[key];
  input.nextElementSibling.classList.toggle('done', input.checked);
  saveActiveFlight(flight);
}

// Dispatch swaps the aircraft when an item makes a leg NO-GO: the new one
// brings its own status and costs a code 46 delay on the first leg.
async function swapAircraft(key) {
  const data = expandedDetailData; if (!data || expandedKey !== key) return;
  const btn = document.getElementById('swapAircraftBtn'); if (btn) btn.disabled = true;
  const exclude = [...new Set(itineraryNogo(data.legs).map(c => c.item).filter(Boolean))];
  try {
    const params = new URLSearchParams({ flights: key, exclude: exclude.join(','), eet: data.legs.map(l => l.eet_minutes || '').join(','), aircraft_type: data.legs[0].aircraft_type || '' });
    const res = await (await fetch('/select/swap-aircraft?' + params)).json();
    if (res.error) throw new Error(res.error);
    data.legs.forEach((leg, i) => { leg.mels = res.mels; leg.cdl = res.cdl; leg.mel = res.mels[0] || null; leg.tech = res.tech[i]; });
    const first = data.legs[0], before = legExpectedDelayMinutes(first);
    first.aircraft_change = first.aircraft_change
      ? { ...first.aircraft_change, minutes: first.aircraft_change.minutes + res.aircraft_change.minutes } : res.aircraft_change;
    first.expected_delay_minutes = before + res.aircraft_change.minutes;
    ['eobt', 'etot', 'eldt', 'eibt'].forEach(f => { if (first[f]) first[f] = shiftZulu(first[f], res.aircraft_change.minutes); });
    revalidateSlot(first);
    for (let k = 1; k < data.legs.length; k++) applyReactionaryDelay(data.legs[k], data.legs[k - 1].eibt, true);
    await paintDetail();
  } catch (err) {
    alert('Could not swap the aircraft. Check the connection and retry');
    if (btn) btn.disabled = false;
  }
}

function buildDetailHtml(data, key, carriers, ownedAircraft) {
  const legBlocks = data.legs.map((leg, i) => `
    <div class="leg-block">
      <div class="leg-block-header">LEG ${i + 1} &mdash; ${leg.flight_number}</div>
      <div class="leg-block-body">
        <table class="kv" style="margin-bottom:10px;">
          <tr><td class="k">DEPARTURE</td><td class="v">${leg.departure_info.icao} / ${leg.departure_info.iata} &mdash; ${leg.departure_info.name} (${leg.departure_info.country})</td></tr>
          <tr><td class="k">ARRIVAL</td><td class="v">${leg.arrival_info.icao} / ${leg.arrival_info.iata} &mdash; ${leg.arrival_info.name} (${leg.arrival_info.country})</td></tr>
        </table>
        ${routeMapDiv(leg, `map-detail-${key}-${i}`)}
        ${legScheduleTable(leg)}
        ${legLoadTable(leg, i, fleetOptionsFor(leg, carriers, ownedAircraft))}
        ${legWeatherBlock(leg)}
        ${legDelayMelBlock(leg)}
        ${opsConstraintsHtml(data.legs, i)}
      </div>
    </div>`).join('');

  const nogo = itineraryNogo(data.legs);
  const banner = nogo.length ? `<div class="nogo-banner" role="alert"><strong>AIRCRAFT NOT DISPATCHABLE FOR THIS TRIP</strong>
      ${nogo.map(c => `Leg ${c.leg + 1}: ${escText(c.text)}`).join('<br>')}
      <div class="nogo-actions"><button class="action small" id="swapAircraftBtn" type="button" onclick="swapAircraft('${key}')">SWAP AIRCRAFT</button>
      <span>Request an aircraft change (expect 20 to 45 min of delay on the first sector) or choose another flight</span></div></div>` : '';
  return `
    <div class="detail-inner">
      ${legBlocks}
      ${banner}
      <div class="confirm-row">
        <button class="action" onclick="confirmFlight('${key}')"${nogo.length ? ' disabled title="This aircraft can&#39;t fly the trip: swap it or pick another flight"' : ''}>CONFIRM</button>
      </div>
    </div>`;
}

async function confirmFlight(key) {
  try {
    // Reuse exactly what was already fetched and reviewed for this
    // itinerary (delay/MEL/pax/cost index etc. are random per /select
    // call) instead of calling /select again, which would silently
    // confirm a different roll than the one the pilot just looked at.
    let selectData = expandedKey === key ? expandedDetailData : null;
    if (!selectData) {
      selectData = await (await fetch('/select?flights=' + encodeURIComponent(key))).json();
    }
    const confirmData = await (await fetch('/confirm?flights=' + encodeURIComponent(key))).json();
    if (selectData.error || confirmData.error) throw new Error(selectData.error || confirmData.error);
    const activeFlight = {
      callsigns: confirmData.callsigns,
      legs: selectData.legs,
      leg_status: confirmData.leg_status,
      leg_state: selectData.legs.map(() => ({
        simbrief: 'not_sent',
        simbrief_static_id: null,
        ofp: null,
        loadsheet_signed: false,
        loadsheet: null,
        pirep: null
      }))
    };
    saveActiveFlight(activeFlight);
    renderRecap(activeFlight);
  } catch (err) {
    alert('Could not confirm this flight. Check the connection and retry');
  }
}

function routeMapDiv(leg, id) {
  const a = leg.departure_info, b = leg.arrival_info;
  if (a.lat == null || a.lon == null || b.lat == null || b.lon == null) return '<div class="placeholder-text">ROUTE MAP: N/A</div>';
  return `<div class="route-map" id="${id}"></div>`;
}

// The fixes of an OFP's planned route, when that OFP belongs to this leg
// (same airports) and carries them; null means draw a straight line.
function ofpRouteFixes(leg, ofp) {
  if (!ofp || !Array.isArray(ofp.navlog) || !ofp.navlog.length) return null;
  const a = leg.departure_info.icao, b = leg.arrival_info.icao;
  if ((ofp.origin_icao && ofp.origin_icao !== a) || (ofp.destination_icao && ofp.destination_icao !== b)) return null;
  // The airports themselves are drawn separately, at both ends.
  return ofp.navlog.filter(f => Number.isFinite(f.lat) && Number.isFinite(f.lon) && f.ident !== a && f.ident !== b);
}

// What the leg map is showing, and why when it's still a straight line:
// no OFP yet, an OFP saved before route drawing existed (offer to fetch
// it again), SimBrief sending no coordinates, or an OFP for other airports.
function routeMapNote(leg, state, refetchIndex) {
  const ofp = state.ofp;
  if (ofpRouteFixes(leg, ofp)) return `Planned route from the SimBrief OFP: ${escText(ofp.route || '')}`;
  const refetch = refetchIndex != null
    ? ` <button class="ghost small" type="button" onclick="fetchOfp(${refetchIndex})">FETCH OFP AGAIN</button>` : '';
  if (!ofp) return 'Straight line between the airports. The planned route appears once the OFP is fetched from SimBrief';
  if (ofp.origin_icao && ofp.destination_icao && (ofp.origin_icao !== leg.departure_info.icao || ofp.destination_icao !== leg.arrival_info.icao)) {
    return `Straight line: the saved OFP is for ${escText(ofp.origin_icao)} &rarr; ${escText(ofp.destination_icao)}, not this leg${refetch}`;
  }
  if (!Array.isArray(ofp.navlog)) return `Straight line: this OFP was saved before routes could be drawn. Fetch it again to draw its route${refetch}`;
  return `Straight line: SimBrief didn't include route coordinates in this OFP.${refetch}`;
}

// Leg route map: the OFP's planned route through its fixes once an OFP is
// loaded (top of climb/descent marked, fix names on hover), otherwise a
// straight dashed line between the airports. opts.interactive adds zoom
// controls (and wheel zoom with a mouse) for the larger maps.
function initRouteMap(id, leg, ofp, opts = {}) {
  const el = document.getElementById(id);
  if (!el || el.dataset.mapInit || typeof L === 'undefined') return;
  const a = leg.departure_info, b = leg.arrival_info;
  if (a.lat == null || a.lon == null || b.lat == null || b.lon == null) return;
  el.dataset.mapInit = '1';
  const interactive = !!opts.interactive;
  const map = L.map(id, {
    zoomControl: interactive, maxZoom: BASEMAP_MAX_ZOOM,
    scrollWheelZoom: interactive && window.matchMedia('(hover: hover) and (pointer: fine)').matches, wheelPxPerZoomLevel: 120,
  });
  addBasemap(map);
  const fixes = ofpRouteFixes(leg, ofp);
  const points = [[a.lat, a.lon], ...(fixes || []).map(f => [f.lat, f.lon]), [b.lat, b.lon]];
  if (fixes) {
    L.polyline(points, { color: '#2563EB', weight: 3, opacity: 0.9 }).addTo(map);
    fixes.forEach(f => {
      const phase = f.ident === 'TOC' || f.ident === 'TOD';
      const m = L.circleMarker([f.lat, f.lon], phase
        ? { radius: 5, color: '#FFFFFF', weight: 2, fillColor: '#DC6803', fillOpacity: 1 }
        : { radius: 3.5, color: '#2563EB', weight: 1.5, fillColor: '#FFFFFF', fillOpacity: 1 }).addTo(map);
      const label = phase ? (f.ident === 'TOC' ? 'TOC · top of climb' : 'TOD · top of descent') : escText(f.ident) + (f.via && f.via !== 'DCT' ? ` · ${escText(f.via)}` : '');
      m.bindTooltip(label, { direction: 'top', offset: [0, -4], className: 'route-fix-tip' });
    });
  } else {
    L.polyline(points, { color: '#2563EB', weight: 2, dashArray: '5,5' }).addTo(map);
  }
  L.marker([a.lat, a.lon]).addTo(map).bindTooltip(a.icao, { permanent: true, direction: 'top', offset: [0, -8] });
  L.marker([b.lat, b.lon]).addTo(map).bindTooltip(b.icao, { permanent: true, direction: 'top', offset: [0, -8] });
  const bounds = L.latLngBounds(points);
  const fit = () => map.fitBounds(bounds, { padding: [26, 26], animate: false });
  fit();
  // Some route maps are sized by the page layout: keep the route framed.
  if (window.ResizeObserver) new ResizeObserver(() => { if (el.isConnected) { map.invalidateSize(); fit(); } }).observe(el);
}

// A workflow step changed: show the leg now in progress, on the tab of its
// next step.
function saveAndRender(flight) {
  recapLegIndex = null;
  recapTab = null;
  saveActiveFlight(flight);
  renderRecap(flight);
}

// Active flight page: leg tabs and the four steps on top (SimBrief, OFP,
// loadsheet, PIREP), the route and schedule on the left, and three tabs on
// the right: BRIEFING, LOADSHEET and PIREP. Which leg and tab are shown
// survives re-renders of the same flight.
let recapLegIndex = null;     // null = the leg in progress
let recapTab = null;          // null = the tab of that leg's next step
let recapFlightKey = '';
const RECAP_TABS = [['briefing', 'BRIEFING'], ['loadsheet', 'LOADSHEET'], ['pirep', 'PIREP']];

function legProgress(flight, i) {
  const done = flight.leg_status[i].status === 'done';
  const current = !done && flight.leg_status.slice(0, i).every(s => s.status === 'done');
  return { done, current };
}
// Where a leg stands: waiting, simbrief, ofp, loadsheet, pirep or done
function legStage(flight, i) {
  const { done, current } = legProgress(flight, i);
  const st = flight.leg_state[i];
  if (done) return 'done';
  if (!current) return 'waiting';
  if (st.simbrief === 'not_sent') return 'simbrief';
  if (st.simbrief !== 'available') return 'ofp';
  return st.loadsheet_signed ? 'pirep' : 'loadsheet';
}
function stageTab(stage) {
  return stage === 'loadsheet' ? 'loadsheet' : stage === 'pirep' || stage === 'done' ? 'pirep' : 'briefing';
}

// The four steps as a read-only checklist: what's done, what's next and
// what's still to do. The actions live where the work is (briefing,
// loadsheet and PIREP tabs).
const STEP_INFO = [
  ['SEND TO SIMBRIEF', 'Open SimBrief with this leg filled in and generate the OFP there'],
  ['FETCH OFP', 'Bring the OFP back: route, fuel and weights complete the briefing'],
  ['SIGN LOADSHEET', 'Ask for the loadsheet, sort out any last-minute changes, sign it'],
  ['FILE PIREP', 'Fly the leg, then report your actual times and fuel'],
];
function afStepsHtml(flight, i) {
  const st = flight.leg_state[i], stage = legStage(flight, i);
  const done = [stage === 'done' || st.simbrief !== 'not_sent', stage === 'done' || st.simbrief === 'available',
    stage === 'done' || !!st.loadsheet_signed, !!st.pirep];
  const nowIndex = ({ simbrief: 0, ofp: 1, loadsheet: 2, pirep: 3 })[stage];
  return `<ol class="af-flow" aria-label="Steps for this leg">${STEP_INFO.map(([label, text], k) => {
    const state = done[k] ? 'done' : k === nowIndex ? 'now' : 'todo';
    const tag = state === 'done' ? 'DONE' : state === 'now' ? 'NEXT' : '';
    return `<li class="af-flow-step ${state}"${state === 'now' ? ' aria-current="step"' : ''}>
      <span class="af-flow-n" aria-hidden="true">${state === 'done' ? '&#10003;' : k + 1}</span>
      <span class="af-flow-body"><b>${label}</b>${tag ? `<span class="af-flow-tag">${tag}</span>` : ''}<small>${text}</small></span></li>`;
  }).join('')}</ol>${stage === 'waiting' ? `<p class="af-flow-note">These steps open once the PIREP for leg ${i} is filed</p>` : ''}`;
}
// The SimBrief actions, at the bottom of the briefing
function briefingActionsHtml(flight, i) {
  const st = flight.leg_state[i], stage = legStage(flight, i);
  if (stage === 'waiting' || stage === 'done' || st.loadsheet_signed) return '';
  if (st.simbrief === 'not_sent') return `<div class="doc-actions brief-actions"><button class="action" onclick="prepareLeg(${i})">SEND TO SIMBRIEF</button><span class="doc-blocker">Opens SimBrief with this leg filled in</span></div>`;
  if (st.simbrief !== 'available') return `<div class="doc-actions brief-actions"><button class="action" onclick="fetchOfp(${i})">FETCH OFP</button><button class="ghost" type="button" onclick="prepareLeg(${i})">SEND TO SIMBRIEF AGAIN</button><span class="doc-blocker">Generate the OFP in the SimBrief tab first</span></div>`;
  return `<div class="doc-actions brief-actions"><button class="ghost" type="button" onclick="fetchOfp(${i})">FETCH OFP AGAIN</button><button class="ghost" type="button" onclick="prepareLeg(${i})">SEND TO SIMBRIEF AGAIN</button></div>`;
}

function renderRecap(flight) {
  setAppNav('flight');
  setPageFit(true);
  if (!flight.leg_state) flight.leg_state = flight.legs.map(() => ({simbrief:'not_sent', simbrief_static_id:null, ofp:null, loadsheet_signed:false, loadsheet:null, pirep:null}));
  const flightKey = flight.legs.map(l => l.flight_number).join(',');
  if (flightKey !== recapFlightKey) { recapFlightKey = flightKey; recapLegIndex = null; recapTab = null; }
  const allDone = flight.leg_status.every(s => s.status === 'done');
  const inProgress = flight.legs.findIndex((_, k) => legProgress(flight, k).current);
  const shownDefault = inProgress >= 0 ? inProgress : flight.legs.length - 1;
  const i = recapLegIndex != null && recapLegIndex < flight.legs.length ? recapLegIndex : shownDefault;
  const leg = flight.legs[i];
  const state = flight.leg_state[i];
  const stage = legStage(flight, i);
  const callsign = flight.callsigns ? flight.callsigns[i] : '';

  const legTabs = flight.legs.map((l, k) => {
    const pr = legProgress(flight, k);
    const status = pr.done ? 'COMPLETE' : pr.current ? 'IN PROGRESS' : 'NEXT';
    return `<button type="button" class="af-leg${k === i ? ' on' : ''}${pr.done ? ' done' : ''}" onclick="selectRecapLeg(${k})" aria-pressed="${k === i}">
      <b>LEG ${k + 1}</b> ${l.flight_number} &middot; ${l.departure_info.icao} &rarr; ${l.arrival_info.icao}<small>${status}</small></button>`;
  }).join('');
  const actions = allDone
    ? `<span class="status-chip status-ok">ALL LEGS COMPLETE</span><button class="action small" onclick="startNewFlight()">NEW FLIGHT</button>`
    : stage !== 'waiting' && stage !== 'done' ? `<button class="action small danger" onclick="skipFlight()">SKIP LEG</button>` : '';
  const mismatch = state.aircraft_type_mismatch && state.ofp ? `<div class="notice-banner">The OFP's aircraft type (${escText(state.ofp.icao_type)}) doesn't match the ${escText(leg.aircraft_type)} assigned to this leg. Double-check the aircraft on SimBrief before flying this OFP</div>` : '';

  const panes = { briefing: briefingHtml(flight, i), loadsheet: loadsheetPaneHtml(flight, i), pirep: pirepPaneHtml(flight, i) };
  const tab = RECAP_TABS.some(([k]) => k === recapTab) ? recapTab : stageTab(stage);
  recapTab = tab;

  const syncNotice = activeSyncNotice ? `<div class="sync-notice" role="status">${activeSyncNotice}</div>` : '';
  activeSyncNotice = '';
  document.getElementById('mainContent').innerHTML = `${syncNotice}${simbriefNoticeHtml()}
    <div class="panel af-top">
      <div class="af-legs">${legTabs}</div>
      <div class="af-status">${actions}</div>
    </div>
    ${mismatch}
    <div class="af-cols">
      <div class="panel af-route">
        <div class="panel-header"><span>${leg.flight_number}${callsign ? ' &mdash; ' + escText(callsign) : ''} &middot; ${leg.departure_info.icao} &rarr; ${leg.arrival_info.icao}</span></div>
        <div class="panel-body">
          ${afStepsHtml(flight, i)}
          <div class="af-route-names">${escText(leg.departure_info.name || '')} &rarr; ${escText(leg.arrival_info.name || '')}</div>
          ${routeMapDiv(leg, `map-recap-${i}`)}
          <p class="af-map-note">${routeMapNote(leg, state, stage !== 'waiting' && stage !== 'done' ? i : null)}</p>
        </div>
      </div>
      <div class="panel af-tabs">
        <div class="af-tabbar" role="tablist">${RECAP_TABS.map(([k, label]) =>
          `<button type="button" role="tab" id="af-tab-${k}" aria-controls="af-pane-${k}" aria-selected="${k === tab}" class="${k === tab ? 'on' : ''}" onclick="selectRecapTab('${k}')">${label}</button>`).join('')}</div>
        ${RECAP_TABS.map(([k]) => `<div class="panel-body af-pane" role="tabpanel" id="af-pane-${k}" aria-labelledby="af-tab-${k}"${k === tab ? '' : ' hidden'}>${panes[k]}</div>`).join('')}
      </div>
    </div>`;
  initRouteMap(`map-recap-${i}`, leg, state.ofp, { interactive: true });
  if (tab === 'pirep' && document.getElementById('delaycoding-' + i)) updateDelayCoding(i);
}

function selectRecapLeg(index) {
  recapLegIndex = index;
  recapTab = null;
  const flight = loadActiveFlight();
  if (flight) renderRecap(flight);
}

// Switching tabs only shows/hides panes, so half-typed forms survive.
function selectRecapTab(key) {
  recapTab = key;
  RECAP_TABS.forEach(([k]) => {
    const btn = document.getElementById('af-tab-' + k), pane = document.getElementById('af-pane-' + k);
    if (!btn || !pane) return;
    btn.classList.toggle('on', k === key);
    btn.setAttribute('aria-selected', k === key ? 'true' : 'false');
    pane.hidden = k !== key;
  });
}

// ---- BRIEFING: everything about the leg, the OFP figures once fetched ----
function kvRows(rows) {
  return `<table class="kv">${rows.map(([k, v]) => `<tr><td class="k">${k}</td><td class="v">${v}</td></tr>`).join('')}</table>`;
}
function ofpUnit(ofp) { return /lb/i.test((ofp && ofp.weight_unit) || '') ? 'LB' : 'KG'; }
function ofpNum(value) {
  if (value === '' || value == null) return null;
  const x = Number(value);
  return Number.isFinite(x) ? x : null;
}
// ---- BRIEFING: a status summary, then the same card layout for every
// section (label on the left, value on the right), in a fixed order:
// flight, timing, technical, OFP, weather.
function sheetRow(label, value, tone = '') {
  return `<div class="sheet-row${tone ? ' ' + tone : ''}"><span class="sheet-k">${label}</span><span class="sheet-v">${value}</span></div>`;
}
function sheetSection(title, rows, extra = '') {
  return `<section class="sheet"><h4 class="sheet-h">${title}</h4>${rows.join('')}${extra}</section>`;
}
function briefingSummary(flight, i) {
  const leg = flight.legs[i];
  const checks = [...((leg.tech && leg.tech.checks) || []), ...legCurfewChecks(leg), dutyCheck(flight.legs, flight.leg_state)].filter(Boolean);
  const nogo = checks.filter(c => c.level === 'nogo').length, caution = checks.filter(c => c.level === 'caution').length;
  const delay = legExpectedDelayMinutes(leg);
  const code = leg.reactionary && leg.reactionary.minutes >= legOwnDelayMinutes(leg) ? '93' : leg.aircraft_change ? '46' : leg.delay ? leg.delay.iata_code : '';
  const mels = legMels(leg).length, cdl = legCdl(leg).length;
  const duty = dutyFigures(flight.legs, flight.leg_state);
  const tiles = [
    ['DISPATCH', nogo ? 'NO-GO' : caution ? 'CAUTION' : 'GO', nogo ? `${nogo} blocking item${nogo > 1 ? 's' : ''}` : caution ? `${caution} item${caution > 1 ? 's' : ''} to watch` : 'All checks clear', nogo ? 'nogo' : caution ? 'caution' : 'ok'],
    ['DELAY', delay ? `+${delay} MIN` : 'ON TIME', delay ? (code ? `IATA ${escText(code)}` : 'Expected delay') : 'No delay expected', delay ? 'caution' : 'ok'],
    ['TECH LOG', mels || cdl ? `${mels} MEL &middot; ${cdl} CDL` : 'CLEAN', mels || cdl ? 'See the technical log' : 'No deferred defects', mels || cdl ? 'caution' : 'ok'],
    ['CREW DUTY', duty ? (duty.over > 0 ? `${fmtMinutes(duty.over)} OVER` : `${fmtMinutes(-duty.over)} SPARE`) : 'N/A',
      duty ? `FDP ${fmtMinutes(duty.fdp)} / ${fmtMinutes(duty.duty.max_fdp_minutes)}` : '', duty ? duty.level : ''],
  ];
  return `<div class="bsum">${tiles.map(([k, v, sub, tone]) => `<div class="bsum-tile ${tone}"><span class="bsum-k">${k}</span><b class="bsum-v">${v}</b><span class="bsum-sub">${sub}</span></div>`).join('')}</div>`;
}
function briefingHtml(flight, i) {
  const leg = flight.legs[i], st = flight.leg_state[i], ofp = st.ofp;
  const callsign = flight.callsigns ? flight.callsigns[i] : '';
  const flightRows = [
    sheetRow('FLIGHT', `${escText(leg.flight_number)}${callsign ? ' &middot; ' + escText(callsign) : ''}`),
    sheetRow('ROUTE', `${leg.departure_info.icao} &rarr; ${leg.arrival_info.icao}`),
    sheetRow('AIRCRAFT', `${escText(leg.aircraft_type || 'N/A')}${ofp && ofp.registration ? ' &middot; ' + escText(ofp.registration) : ''}`),
    sheetRow('PASSENGERS', `${fmtNum(leg.pax_count)} of ${fmtNum(leg.seat_capacity)} seats (${Math.round((leg.load_factor || 0) * 100)}%)`),
    sheetRow('BAGS (EXPECTED)', `${fmtNum(leg.cargo_weight_kg)} kg`),
  ];
  return briefingSummary(flight, i)
    + sheetSection('FLIGHT', flightRows)
    + sheetSection('TIMING AND CREW', timingRows(flight, i))
    + sheetSection('TECHNICAL LOG', techRows(leg), techCards(flight, i))
    + sheetSection('SIMBRIEF OFP', ofpRows(st))
    + sheetSection('WEATHER', weatherRows(leg))
    + briefingActionsHtml(flight, i);
}
function timingRows(flight, i) {
  const leg = flight.legs[i], rows = [];
  const own = legOwnDelayMinutes(leg);
  const time = (label, e, sch) => rows.push(sheetRow(label, `${escText(e || 'N/A')} <span class="sheet-note">scheduled ${escText(sch || 'N/A')}</span>`));
  time('OFF-BLOCK', leg.eobt, leg.sobt);
  time('TAKE-OFF', leg.etot, leg.stot);
  time('LANDING', leg.eldt, leg.sldt);
  time('IN-BLOCK', leg.eibt, leg.sibt);
  if (leg.eet_minutes != null) rows.push(sheetRow('FLIGHT TIME (EET)', fmtMinutes(leg.eet_minutes)));
  if (leg.reactionary) rows.push(sheetRow(`DELAY +${leg.reactionary.minutes} MIN`, `${leg.reactionary.expected ? 'Late inbound aircraft expected from the previous sector' : 'Late inbound aircraft from the previous sector'} <span class="sheet-note">IATA 93</span>`, 'caution'));
  if (leg.delay) rows.push(sheetRow(`DELAY +${Number.isFinite(leg.delay.minutes) ? leg.delay.minutes : Math.max(...leg.delay.duration_range_minutes)} MIN`, `${escText(leg.delay.plain || leg.delay.description)} <span class="sheet-note">IATA ${escText(leg.delay.iata_code)}</span>`, 'caution'));
  if (leg.aircraft_change) rows.push(sheetRow(`DELAY +${leg.aircraft_change.minutes} MIN`, 'Aircraft change <span class="sheet-note">IATA 46</span>', 'caution'));
  if (leg.reactionary && own) rows.push(sheetRow('TOTAL DELAY', `${legExpectedDelayMinutes(leg)} min: the other delays are absorbed during the late turnaround`));
  if (leg.atfm) rows.push(sheetRow(`ATC SLOT ${escText(leg.atfm.ctot)}`, `Take off between ${slotWindowText(leg.atfm)} <span class="sheet-note">${escText(leg.atfm.reason.toLowerCase())}, ${escText(leg.atfm.where)}${leg.atfm.revised ? ', new slot' : ''}</span>`, 'caution'));
  legCurfewChecks(leg).forEach(c => rows.push(sheetRow(`CURFEW ${escText(c.icao)}`, c.text, c.level)));
  const duty = dutyFigures(flight.legs, flight.leg_state);
  if (duty) {
    const d = duty.duty;
    rows.push(sheetRow('CREW REPORT', `${d.report_local} local <span class="sheet-note">${d.report_utc}</span>`));
    rows.push(sheetRow('MAX FDP', `${fmtMinutes(d.max_fdp_minutes)} for ${d.sectors} sector${d.sectors > 1 ? 's' : ''} <span class="sheet-note">EASA ORO.FTL.205</span>`));
    rows.push(sheetRow('EXPECTED FDP', `${fmtMinutes(duty.fdp)} <span class="sheet-note">${duty.over > 0 ? `${fmtMinutes(duty.over)} into commander's discretion` : `${fmtMinutes(-duty.over)} to spare`}</span>`, duty.level));
  }
  return rows;
}
const CHECK_LABELS = { nogo: 'NO-GO', caution: 'CAUTION', ok: 'CLEAR' };
function techRows(leg) {
  const mels = legMels(leg), cdl = legCdl(leg);
  if (!mels.length && !cdl.length) return [sheetRow('STATUS', 'No deferred defects or missing panels', 'ok')];
  const rows = ((leg.tech && leg.tech.checks) || []).map(c => sheetRow(CHECK_LABELS[c.level] || 'CHECK', escText(c.text), c.level));
  const t = leg.tech || {}, plan = [];
  if (t.max_fl) plan.push(`cruise FL${t.max_fl} or below`);
  if (t.extra_fuel_min) plan.push(`+${t.extra_fuel_min} min fuel`);
  plan.push('every item in full in the dispatch remarks');
  rows.push(sheetRow('IN SIMBRIEF PLAN', plan.join(' &middot; ')));
  return rows;
}
// One card per deferred item, in the same order for each: reference and
// name, deadline, what it means, the crew's steps, the official wording.
function techCards(flight, i) {
  const leg = flight.legs[i], done = (flight.leg_state[i] && flight.leg_state[i].tech_done) || {};
  const step = (key, text) => `<li><label><input type="checkbox" ${done[key] ? 'checked' : ''} onchange="toggleTechStep(${i}, '${escAttr(key)}', this)"><span class="${done[key] ? 'done' : ''}">${escText(text)}</span></label></li>`;
  const card = (ref, m, steps) => {
    const official = (m.procedures || []).map(t => `<li>${escText(stripMO(t))}</li>`).join('');
    return `<div class="tcard">
      <div class="tcard-head"><span class="tech-ref">${ref}</span><b>${techTitle(m)}</b></div>
      <p class="tcard-means">${escText(m.plain || m.description)}</p>
      ${steps.length ? `<ul class="tech-steps">${steps.map((t, k) => step(`${m.id}#${k}`, t)).join('')}</ul>` : ''}
      ${official || m.description ? `<details class="tech-official"><summary>Official wording</summary><p>${escText(m.description)}</p>${official ? `<ul>${official}</ul>` : ''}${m.source ? `<p>Source: ${escText(m.source)}</p>` : ''}</details>` : ''}
    </div>`;
  };
  const cards = legMels(leg).map(m => card(`MEL ${techRef(m)}`, m, m.steps || (m.procedures || []).map(stripMO)))
    .concat(legCdl(leg).map(c => {
      const e = c.effects || {};
      const steps = c.steps || [e.fuel_burn_pct ? `Burns ${e.fuel_burn_pct}% more fuel: already in the flight plan` : '',
        e.weight_penalty_kg ? `Maximum take-off and landing weights reduced by ${e.weight_penalty_kg} kg` : ''].filter(Boolean);
      return card(`CDL ${techRef(c)}`, c, steps);
    }));
  return cards.length ? `<div class="tcards">${cards.join('')}</div>` : '';
}
function ofpRows(st) {
  const ofp = st.ofp;
  if (!ofp) return [sheetRow('STATUS', 'Not fetched yet: the route, fuel and weights appear here once the OFP is in')];
  const unit = ofpUnit(ofp), w = v => ofpNum(v) != null ? `${fmtNum(v)} ${unit.toLowerCase()}` : 'N/A';
  const fl = ofpNum(ofp.initial_altitude_ft);
  return [
    sheetRow('ROUTE', `<span class="mono">${escText(ofp.route || 'N/A')}</span>`),
    sheetRow('INITIAL CRUISE', fl ? `FL${Math.round(fl / 100)}` : 'N/A'),
    sheetRow('COST INDEX', ofp.cost_index !== '' ? fmtNum(ofp.cost_index) : 'N/A'),
    sheetRow('BLOCK FUEL', w(ofp.block_fuel)),
    sheetRow('TRIP FUEL', w(ofp.trip_fuel || ofp.plan_trip_fuel)),
    sheetRow('TAKE-OFF FUEL', w(ofp.takeoff_fuel)),
    sheetRow('EST. ZERO FUEL WT', w(ofp.est_zfw)),
    sheetRow('EST. TAKE-OFF WT', w(ofp.est_tow)),
    sheetRow('EST. LANDING WT', w(ofp.est_ldw)),
  ];
}
function weatherRows(leg) {
  const wx = (icao, kind, text) => sheetRow(`${icao} ${kind}`, `<span class="mono">${escText(text || 'Unavailable')}</span>`);
  return [wx(leg.departure_info.icao, 'METAR', leg.weather.departure.metar), wx(leg.departure_info.icao, 'TAF', leg.weather.departure.taf),
    wx(leg.arrival_info.icao, 'METAR', leg.weather.arrival.metar), wx(leg.arrival_info.icao, 'TAF', leg.weather.arrival.taf)];
}

function ofpSectionHtml(leg, st) {
  const ofp = st.ofp;
  if (!ofp) return `<p class="brief-empty">The route, fuel and weights appear here once the OFP is fetched from SimBrief</p>`;
  const unit = ofpUnit(ofp), w = v => ofpNum(v) != null ? `${fmtNum(v)} ${unit}` : 'N/A';
  const fl = ofpNum(ofp.initial_altitude_ft);
  return kvRows([
    ['ROUTE', `<span class="mono">${escText(ofp.route || 'N/A')}</span>`],
    ['INITIAL CRUISE', fl ? `FL${Math.round(fl / 100)}` : 'N/A'],
    ['COST INDEX', ofp.cost_index !== '' ? fmtNum(ofp.cost_index) : 'N/A'],
    ['BLOCK FUEL', w(ofp.block_fuel)],
    ['TRIP FUEL', w(ofp.trip_fuel || ofp.plan_trip_fuel)],
    ['TAKE-OFF FUEL', w(ofp.takeoff_fuel)],
    ['EZFW / ETOW / ELDW', `${w(ofp.est_zfw)} / ${w(ofp.est_tow)} / ${w(ofp.est_ldw)}`],
    ['PASSENGERS (OFP)', ofp.epax !== '' ? fmtNum(ofp.epax) : 'N/A'],
  ]);
}
// ---- LOADSHEET ----
// Asked for once the OFP is in. Only then do the last-minute changes and
// dangerous goods turn up; the captain accepts or declines what can be
// declined, and signs once everything is sorted and within limits.
const STD_BAG_KG = 15;
function kgToUnit(unit, kg) { return unit === 'LB' ? Math.round(kg * 2.20462) : Math.round(kg); }
function unitToKg(unit, v) { return unit === 'LB' ? Math.round(v * 0.45359237) : Math.round(v); }

// The loadsheet before any change, from the OFP. null when the OFP has no
// usable weights.
function loadsheetBase(flight, i) {
  const leg = flight.legs[i], ofp = flight.leg_state[i].ofp || {}, unit = ofpUnit(ofp);
  const zfwOfp = ofpNum(ofp.est_zfw), towOfp = ofpNum(ofp.est_tow);
  const pax = ofpNum(ofp.epax) != null ? ofpNum(ofp.epax) : leg.pax_count;
  const paxW = ofpNum(ofp.pax_weight) || kgToUnit(unit, 84);
  let payload = ofpNum(ofp.payload), dow = ofpNum(ofp.dow);
  if (payload == null && zfwOfp != null && dow != null) payload = zfwOfp - dow;
  if (payload == null) payload = pax * paxW + kgToUnit(unit, leg.cargo_weight_kg || 0);
  if (dow == null && zfwOfp != null) dow = zfwOfp - payload;
  if (dow == null) return null;
  let tof = ofpNum(ofp.takeoff_fuel);
  if (tof == null && zfwOfp != null && towOfp != null) tof = towOfp - zfwOfp;
  if (tof == null && ofpNum(ofp.block_fuel) != null) tof = ofpNum(ofp.block_fuel) - (ofpNum(ofp.taxi_fuel) || 0);
  if (tof == null) return null;
  let trip = ofpNum(ofp.trip_fuel) != null ? ofpNum(ofp.trip_fuel) : ofpNum(ofp.plan_trip_fuel);
  if (trip == null && towOfp != null && ofpNum(ofp.est_ldw) != null) trip = towOfp - ofpNum(ofp.est_ldw);
  const penalty = kgToUnit(unit, (leg.tech && leg.tech.weight_penalty_kg) || 0);
  const paxLoad = Math.round(pax * paxW);
  return {
    unit, pax, paxW, dow, tof, trip, taxi: ofpNum(ofp.taxi_fuel) || 0, paxLoad, hold: Math.max(0, Math.round(payload - paxLoad)),
    maxZfw: ofpNum(ofp.max_zfw), maxTow: ofpNum(ofp.max_tow) != null ? ofpNum(ofp.max_tow) - penalty : null,
    maxLdw: ofpNum(ofp.max_ldw) != null ? ofpNum(ofp.max_ldw) - penalty : null, penalty,
    seats: leg.seat_capacity, cabinCrew: Math.max(1, Math.ceil((leg.seat_capacity || 0) / 50)),
  };
}
// Weights with a set of changes applied, and the margin to the limits
function loadsheetFigures(b, changes) {
  const sum = key => changes.reduce((a, c) => a + (c[key] || 0), 0);
  const pax = b.pax + sum('pax'), paxLoad = b.paxLoad + sum('paxLoad'), hold = Math.max(0, b.hold + sum('hold'));
  const tof = b.tof + sum('fuel');
  const zfw = b.dow + paxLoad + hold, tow = zfw + tof, lw = b.trip != null ? tow - b.trip : null;
  const limits = [];
  if (b.maxZfw != null) limits.push(['Z', b.maxZfw + tof]);
  if (b.maxTow != null) limits.push(['T', b.maxTow]);
  if (b.maxLdw != null && b.trip != null) limits.push(['L', b.maxLdw + b.trip]);
  const lim = limits.length ? limits.reduce((a, c) => (c[1] < a[1] ? c : a)) : null;
  return { pax, paxLoad, hold, tof, zfw, tow, lw, limitBy: lim ? lim[0] : '', underload: lim ? Math.round(lim[1] - tow) : null };
}
function isNoDg(dg) { return !dg || /^no dangerous goods/i.test(dg.label || ''); }
// What the captain has to deal with before signing. Each item: a tag, the
// text, its choices and what each choice does to the load.
function loadsheetItems(flight, i, b) {
  const leg = flight.legs[i], ls = flight.leg_state[i].ls || {}, unit = b.unit, items = [];
  const lmc = ls.lmc_event;
  if (lmc) {
    const pax = lmc.type === 'pax_change';
    let d = lmc.delta;
    if (pax && d > 0) d = Math.min(d, Math.max(0, (b.seats || b.pax) - b.pax));
    if (d) {
      const paxLoad = pax ? Math.round(d * b.paxW) : 0;
      const hold = pax ? (d < 0 ? kgToUnit(unit, d * STD_BAG_KG) : 0) : kgToUnit(unit, d);
      const change = { label: pax ? (d > 0 ? 'LATE PAX' : 'PAX OFFLOAD') : (d > 0 ? 'EXTRA BAGS' : 'BAGS OFFLOAD'), pax: pax ? d : 0, paxLoad, hold, weight: paxLoad + hold };
      const amount = pax ? `${d > 0 ? '+' : ''}${d} passenger${Math.abs(d) === 1 ? '' : 's'}${d < 0 ? ' and their bags' : ''}` : `${d > 0 ? '+' : ''}${fmtNum(kgToUnit(unit, d))} ${unit} in the hold`;
      items.push(d > 0
        ? { key: 'lmc', tag: 'LMC', text: `${escText(lmc.description)}: <b>${amount}</b> (${fmtNum(change.weight)} ${unit})`, choices: [['accept', 'ACCEPT'], ['decline', 'DECLINE']], change: { accept: change } }
        : { key: 'lmc', tag: 'LMC', text: `${escText(lmc.description)}: <b>${amount}</b> (${fmtNum(change.weight)} ${unit}). Unaccompanied bags always come off the aircraft`, choices: [['ack', 'ACKNOWLEDGE']], change: { ack: change } });
    }
  }
  const dg = ls.dangerous_goods;
  if (!isNoDg(dg)) {
    if (dg.notoc) {
      const w = kgToUnit(unit, dg.weight_kg || 0);
      items.push({ key: 'dg', tag: 'NOTOC', text: `Dangerous goods for the hold: <b>${escText(dg.label)}</b>. Accept and sign the NOTOC, or refuse and have it offloaded`,
        choices: [['accept', 'ACCEPT NOTOC'], ['refuse', 'REFUSE']], change: { accept: { label: `DG ${dg.un || ''}`.trim(), hold: w, weight: w } } });
    } else {
      items.push({ key: 'dg', tag: 'DG INFO', text: `${escText(dg.label)}. Carried within the passenger allowances: the captain is informed, nothing to sign`, choices: [['ack', 'ACKNOWLEDGE']], change: {} });
    }
  }
  // MEL fuel limits (see techlog.py): fuel-pump minimums, centre tank empty
  const tech = leg.tech || {};
  if (tech.min_takeoff_fuel_kg && unitToKg(unit, b.tof) < tech.min_takeoff_fuel_kg) {
    const add = Math.ceil((kgToUnit(unit, tech.min_takeoff_fuel_kg) - b.tof) / 100) * 100;
    items.push({ key: 'fuelmin', tag: 'MEL FUEL', text: `Take-off fuel ${fmtNum(b.tof)} ${unit} is below the MEL minimum of ${fmtNum(kgToUnit(unit, tech.min_takeoff_fuel_kg))} ${unit} (fuel pump inoperative). Uplift ${fmtNum(add)} ${unit} more`,
      choices: [['ack', 'UPLIFT FUEL']], change: { ack: { label: 'FUEL UPLIFT', fuel: add, weight: add } } });
  }
  if (tech.max_fuel_kg && unitToKg(unit, b.tof + b.taxi) > tech.max_fuel_kg) {
    const cut = Math.ceil((b.tof + b.taxi - kgToUnit(unit, tech.max_fuel_kg)) / 100) * 100;
    items.push({ key: 'fuelmax', tag: 'MEL FUEL', text: `Block fuel is above the ${fmtNum(kgToUnit(unit, tech.max_fuel_kg))} ${unit} the main tanks hold (centre tank unusable). Defuel ${fmtNum(cut)} ${unit}`,
      choices: [['ack', 'DEFUEL']], change: { ack: { label: 'FUEL DEFUEL', fuel: -cut, weight: -cut } } });
  }
  return items;
}
function loadsheetState(flight, i) {
  const b = loadsheetBase(flight, i);
  if (!b) return null;
  const items = loadsheetItems(flight, i, b);
  const decisions = (flight.leg_state[i].ls || {}).decisions || {};
  const applied = items.map(it => it.change[decisions[it.key]]).filter(Boolean);
  const fig = loadsheetFigures(b, applied);
  const before = loadsheetFigures(b, []);
  const pending = items.filter(it => !decisions[it.key]);
  return { b, items, decisions, applied, fig, before, pending, over: fig.underload != null && fig.underload < 0 };
}

const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
function opsDate(iso) {
  const d = iso ? new Date(iso) : new Date();
  return `${String(d.getUTCDate()).padStart(2, '0')}${MONTHS[d.getUTCMonth()]}${String(d.getUTCFullYear()).slice(2)}`;
}
function opsTime(iso) {
  const d = iso ? new Date(iso) : new Date();
  return String(d.getUTCHours()).padStart(2, '0') + String(d.getUTCMinutes()).padStart(2, '0');
}
// The loadsheet as the printed document: fixed-width lines
function loadsheetText(flight, i, s, signedAt) {
  const leg = flight.legs[i], st = flight.leg_state[i], ofp = st.ofp || {}, b = s.b, f = s.fig;
  const pad = (t, w) => String(t).padEnd(w).slice(0, w), num = (v, w = 9) => (v == null ? 'N/A' : String(Math.round(v))).padStart(w);
  const rule = '-'.repeat(64);
  const edition = s.applied.length ? '02' : '01';
  const max = (v, mark) => v == null ? '' : `   MAX ${num(v, 7)}${f.limitBy === mark ? '  L' : ''}`;
  const fwd = Math.round(f.hold * 0.4);
  const lines = [
    `LOADSHEET           CHECKED     APPROVED                 EDNO`,
    `ALL WEIGHTS IN ${b.unit}    VDISPATCH   ${pad(signedAt ? 'CAPT ' + opsTime(signedAt) + 'Z' : '', 25)}${edition}`,
    '',
    `FROM/TO   FLIGHT    A/C REG    VERSION  CREW  DATE     TIME`,
    `${pad(leg.departure_info.icao + '/' + leg.arrival_info.icao, 10)}${pad(leg.flight_number, 10)}${pad(ofp.registration || 'N/A', 11)}${pad('Y' + (b.seats || ''), 9)}${pad('2/' + b.cabinCrew, 6)}${pad(opsDate(st.ls && st.ls.requested_at), 9)}${opsTime(st.ls && st.ls.requested_at)}`,
    '',
    `                          WEIGHT    DISTRIBUTION`,
    `LOAD IN COMPARTMENTS   ${num(f.hold)}    FWD/${fwd} AFT/${f.hold - fwd}`,
    `PASSENGER/CABIN BAG    ${num(f.paxLoad)}    ${f.pax} PAX   TTL ${f.pax}`,
    rule,
    `TOTAL TRAFFIC LOAD     ${num(f.paxLoad + f.hold)}`,
    `DRY OPERATING WEIGHT   ${num(b.dow)}`,
    `ZERO FUEL WEIGHT ACT   ${num(f.zfw)}${max(b.maxZfw, 'Z')}`,
    `TAKE OFF FUEL          ${num(f.tof)}`,
    `TAKE OFF WEIGHT ACT    ${num(f.tow)}${max(b.maxTow, 'T')}`,
    `TRIP FUEL              ${num(b.trip)}`,
    `LANDING WEIGHT ACT     ${num(f.lw)}${max(b.maxLdw, 'L')}`,
    rule,
    `UNDERLOAD BEFORE LMC   ${num(s.before.underload)}`,
    `UNDERLOAD AFTER LMC    ${num(f.underload)}${s.over ? '   LIMITS EXCEEDED' : ''}`,
    rule,
    `LAST MINUTE CHANGES`,
    `DEST  SPECIFICATION              +/-         WEIGHT`,
  ];
  s.items.forEach(it => {
    const dec = s.decisions[it.key], ch = it.change[dec];
    const spec = it.change.accept ? it.change.accept.label : it.change.ack ? it.change.ack.label : it.tag;
    if (!dec) lines.push(`${pad(leg.arrival_info.icao, 6)}${pad(spec, 25)}PENDING CAPTAIN DECISION`);
    else if (ch && ch.weight) lines.push(`${pad(leg.arrival_info.icao, 6)}${pad(ch.label, 25)}${ch.weight > 0 ? '+' : '-'}${num(Math.abs(ch.weight), 16)}`);
    else lines.push(`${pad(leg.arrival_info.icao, 6)}${pad(spec, 25)}${dec === 'ack' ? 'NOTED' : 'DECLINED'}`);
  });
  if (!s.items.length) lines.push('NIL');
  const lmcTotal = s.applied.reduce((a, c) => a + (c.weight || 0), 0);
  lines.push(`LMC TOTAL                        ${lmcTotal >= 0 ? '+' : '-'}${num(Math.abs(lmcTotal), 16)}`, rule,
    'LOADMESSAGE AND CAPTAINS INFORMATION');
  const dg = st.ls && st.ls.dangerous_goods;
  if (isNoDg(dg)) lines.push('SI NOTOC: NIL');
  else if (dg.notoc) lines.push(`SI NOTOC: ${dg.un} CL ${dg['class']} ${kgToUnit(b.unit, dg.weight_kg)} ${b.unit} HOLD ${dg.hold}${s.decisions.dg === 'refuse' ? ' OFFLOADED' : s.decisions.dg ? ' ACCEPTED' : ''}`);
  else lines.push(`SI INFO: ${dg.label.toUpperCase()}`);
  legMels(leg).forEach(m => lines.push(`   MEL ${m.ata || ''} ${String(m.description || '').toUpperCase()}`));
  legCdl(leg).forEach(c => lines.push(`   CDL ${c.ata || ''} ${String(c.part || c.description || '').toUpperCase()}`));
  if (b.penalty) lines.push(`   MTOW AND MLW REDUCED BY ${b.penalty} ${b.unit} (CDL)`);
  lines.push(rule, signedAt ? `SIGNED BY THE CAPTAIN ${opsDate(signedAt)} ${opsTime(signedAt)}Z` : 'CAPTAINS SIGNATURE ............');
  return lines.join('\n');
}

function docEmpty(title, text, button) {
  return `<div class="doc-empty"><h3>${title}</h3><p>${text}</p>${button}</div>`;
}
function loadsheetPaneHtml(flight, i) {
  const st = flight.leg_state[i], stage = legStage(flight, i);
  if (st.loadsheet_signed && st.loadsheet && st.loadsheet.text) {
    return `<pre class="doc-sheet">${escText(st.loadsheet.text)}</pre>`;
  }
  if (st.loadsheet_signed) {   // signed before loadsheets were documents
    const l = st.loadsheet || {};
    return kvRows([['ZFW', fmtNum(l.zfw)], ['FUEL ON BOARD', fmtNum(l.fob)], ['PASSENGERS', fmtNum(l.pax)], ['CREW', fmtNum(l.crew)]]);
  }
  if (stage === 'waiting') return docEmpty('No loadsheet yet', `This leg opens once the PIREP for leg ${i} is filed`, '');
  if (st.simbrief !== 'available') return docEmpty('No loadsheet yet', 'The loadsheet is prepared from the OFP: send the leg to SimBrief and fetch the OFP first', '<button class="action" disabled>ASK FOR LOADSHEET</button>');
  if (!st.ls) return docEmpty('Ready for the loadsheet', 'Load control prepares it from your OFP. Last-minute changes and dangerous goods, if any, turn up with it', `<button class="action" onclick="askForLoadsheet(${i})">ASK FOR LOADSHEET</button>`);
  const s = loadsheetState(flight, i);
  if (!s) return docEmpty('No weights in this OFP', 'SimBrief sent no weights with this OFP. Fetch the OFP again, then ask for the loadsheet', `<button class="action" onclick="fetchOfp(${i})">FETCH OFP AGAIN</button>`);
  const itemsHtml = s.items.map(it => {
    const dec = s.decisions[it.key];
    const buttons = it.choices.map(([id, label]) => {
      const trial = it.change[id] ? loadsheetFigures(s.b, s.items.filter(o => o.key !== it.key).map(o => o.change[s.decisions[o.key]]).filter(Boolean).concat(it.change[id])) : null;
      const tooHeavy = trial && trial.underload != null && trial.underload < 0 && (it.change[id].weight || 0) > 0;
      return `<button type="button" class="${dec === id ? 'action' : 'ghost'} small" onclick="decideLoadsheetItem(${i}, '${it.key}', '${id}')"${tooHeavy ? ' disabled title="Over the weight limits: not possible"' : ''}>${label}</button>`;
    }).join('');
    return `<div class="ls-item${dec ? ' done' : ''}"><span class="brief-tag">${it.tag}</span><div class="ls-item-body"><p>${it.text}</p><div class="ls-item-actions">${buttons}${dec ? '<span class="ls-item-ok">&#10003; sorted</span>' : ''}</div></div></div>`;
  }).join('');
  const blocker = s.pending.length ? `${s.pending.length} item${s.pending.length > 1 ? 's' : ''} still to sort out` : s.over ? 'The load is over the weight limits' : '';
  return `<pre class="doc-sheet" id="loadsheet-${i}">${escText(loadsheetText(flight, i, s, null))}</pre>
    ${s.items.length ? `<h4 class="ls-items-title">CAPTAIN'S DECISIONS</h4><div class="ls-items">${itemsHtml}</div>` : '<p class="brief-empty">No last-minute changes and no dangerous goods to sign for</p>'}
    <div class="doc-actions"><button class="action" id="signLoadsheetBtn-${i}" onclick="signLoadsheet(${i})"${blocker ? ' disabled' : ''}>SIGN LOADSHEET</button>${blocker ? `<span class="doc-blocker">${blocker}</span>` : ''}</div>`;
}
async function askForLoadsheet(i) {
  const flight = loadActiveFlight(); if (!flight) return;
  const leg = flight.legs[i];
  const full = leg.pax_count >= leg.seat_capacity ? 1 : 0;
  try {
    const res = await (await fetch(`/loadsheet/extras?flight=${encodeURIComponent(leg.flight_number)}&full=${full}`)).json();
    if (res.error) throw new Error(res.error);
    flight.leg_state[i].ls = { requested_at: new Date().toISOString(), lmc_event: res.lmc_event, dangerous_goods: res.dangerous_goods, decisions: {} };
    recapTab = 'loadsheet';
    saveActiveFlight(flight);
    renderRecap(flight);
  } catch (err) {
    alert('Could not get the loadsheet. Check the connection and retry');
  }
}
function decideLoadsheetItem(i, key, choice) {
  const flight = loadActiveFlight(); if (!flight || !flight.leg_state[i].ls) return;
  flight.leg_state[i].ls.decisions[key] = choice;
  recapTab = 'loadsheet';
  saveActiveFlight(flight);
  renderRecap(flight);
}
function signLoadsheet(i) {
  const flight = loadActiveFlight(); if (!flight) return;
  const s = loadsheetState(flight, i);
  if (!s || s.pending.length || s.over) return;
  const signedAt = new Date().toISOString(), leg = flight.legs[i], f = s.fig;
  flight.leg_state[i].loadsheet = {
    edition: s.applied.length ? 2 : 1, unit: s.b.unit, zfw: f.zfw, tow: f.tow, lw: f.lw, tof: f.tof, fob: f.tof,
    pax: f.pax, crew: 2 + s.b.cabinCrew, hold: f.hold, underload: f.underload,
    changes: s.applied.map(c => ({ label: c.label, weight: c.weight })), decisions: { ...s.decisions },
    signed_at: signedAt, text: loadsheetText(flight, i, s, signedAt),
  };
  flight.leg_state[i].loadsheet_signed = true;
  // The flown load, for the logbook
  leg.pax_count = f.pax;
  if (leg.seat_capacity) leg.load_factor = Math.round((f.pax / leg.seat_capacity) * 100) / 100;
  saveAndRender(flight);
}

// ---- PIREP ----
function pirepTimeRow(id, label, ref, onInput) {
  const handler = onInput ? ` oninput="${onInput}"` : '';
  return `<div class="doc-row"><label class="dk" for="${id}-hh">${label}</label>
    <span class="doc-time"><input id="${id}-hh" type="number" min="0" max="23" placeholder="HH" inputmode="numeric" aria-label="${label} hours"${handler}>:<input id="${id}-mm" type="number" min="0" max="59" placeholder="MM" inputmode="numeric" aria-label="${label} minutes"${handler}>Z</span>
    <span class="dh">${ref}</span></div>`;
}
function pirepDocHead(flight, i) {
  const leg = flight.legs[i], st = flight.leg_state[i], ofp = st.ofp || {};
  const callsign = flight.callsigns ? flight.callsigns[i] : '';
  return `<div class="doc-head"><b>PILOT REPORT</b><span>${escText(leg.flight_number)}${callsign ? ' / ' + escText(callsign) : ''}</span></div>
    <div class="doc-meta">${leg.departure_info.icao}/${leg.arrival_info.icao} &middot; ${opsDate(st.loadsheet && st.loadsheet.signed_at)} &middot; ${escText(leg.aircraft_type || '')}${ofp.registration ? ' ' + escText(ofp.registration) : ''}</div>`;
}
function pirepPaneHtml(flight, i) {
  const leg = flight.legs[i], st = flight.leg_state[i], ofp = st.ofp || {}, unit = ofpUnit(st.ofp);
  if (st.pirep) return pirepFiledHtml(flight, i);
  if (!st.loadsheet_signed) return docEmpty('No PIREP yet', 'The PIREP opens once the loadsheet for this leg is signed', '<button class="action" disabled>FILL PIREP</button>');
  if (!st.pirep_open) return docEmpty('Ready for the PIREP', 'Fly the leg, then report your actual times and the fuel left at the destination', `<button class="action" onclick="openPirep(${i})">FILL PIREP</button>`);
  const exp = (s, e) => `SCHED ${s || 'N/A'} &middot; EXP ${e || 'N/A'}`;
  const slot = leg.atfm ? ` &middot; SLOT ${escText(leg.atfm.ctot)}` : '';
  const planFuel = ofpNum(ofp.plan_landing_fuel) != null ? `PLAN ${fmtNum(ofp.plan_landing_fuel)} ${unit}` : '';
  return `<div class="doc pirep-doc" id="pirep-${i}">
    ${pirepDocHead(flight, i)}
    <div class="doc-rows">
      ${pirepTimeRow(`aobt-${i}`, 'BLOCK OFF  AOBT', exp(leg.sobt, leg.eobt), `updateDelayCoding(${i})`)}
      ${pirepTimeRow(`atot-${i}`, 'TAKE OFF   ATOT', exp(leg.stot, leg.etot) + slot)}
      ${pirepTimeRow(`aldt-${i}`, 'LANDING    ALDT', exp(leg.sldt, leg.eldt))}
      ${pirepTimeRow(`abit-${i}`, 'BLOCK ON   AIBT', exp(leg.sibt, leg.eibt))}
      <div class="doc-row"><label class="dk" for="afad-${i}">FUEL AT DEST AFAD</label>
        <span class="doc-time"><input id="afad-${i}" class="wide" type="number" min="0" inputmode="numeric">${unit}</span><span class="dh">${planFuel}</span></div>
    </div>
    <div class="delay-coding" id="delaycoding-${i}"></div>
    <label class="doc-check"><input id="normal-${i}" type="checkbox"> NO NEW DEFECTS, MEL ITEMS OR NON-NORMAL OCCURRENCES TO ENTER IN THE TECH LOG</label>
    <label class="doc-remarks" for="remarks-${i}">PILOT REMARKS</label>
    <textarea id="remarks-${i}" class="doc-textarea" maxlength="500" rows="3" placeholder="Anything worth noting about this flight (optional)"></textarea>
    <div class="doc-actions"><button class="action" onclick="submitPirep(${i})">SIGN PIREP</button>
      <button class="ghost" type="button" onclick="sendToAircraftSoon(${i})" title="Coming soon">SEND TO AIRCRAFT <span class="soon-tag">SOON</span></button>
      <span class="doc-blocker" id="sta-note-${i}" hidden>Coming soon: the PIREP will go to and from the aircraft directly</span></div>
  </div>`;
}
function pirepFiledHtml(flight, i) {
  const st = flight.leg_state[i], p = st.pirep, unit = ofpUnit(st.ofp);
  const rows = [['BLOCK OFF  AOBT', `${p.aobt}Z`], ['TAKE OFF   ATOT', `${p.atot}Z`], ['LANDING    ALDT', `${p.aldt}Z`], ['BLOCK ON   AIBT', `${p.abit}Z`],
    ['FUEL AT DEST AFAD', `${fmtNum(p.afad)} ${unit}`]];
  if (p.delay_codes && p.delay_codes.length) rows.push(['DELAY CODES', p.delay_codes.map(c => `${escText(c.code)}/${c.minutes}`).join('  ')]);
  if (p.slot) rows.push(['ATC SLOT', `${p.slot.met ? 'MET' : 'MISSED'} (${p.slot.delta >= 0 ? '+' : ''}${p.slot.delta} MIN ON CTOT ${escText(p.slot.ctot)})`]);
  rows.push(['TECH LOG', p.no_new_mel_or_non_normal ? 'NIL NEW DEFECTS' : 'N/A']);
  return `<div class="doc pirep-doc filed">${pirepDocHead(flight, i)}
    <div class="doc-rows">${rows.map(([k, v]) => `<div class="doc-row"><span class="dk">${k}</span><span>${v}</span></div>`).join('')}</div>
    ${p.remarks ? `<div class="doc-remarks">PILOT REMARKS</div><p class="doc-remarks-text">${escText(p.remarks)}</p>` : ''}
    <div class="doc-meta">SIGNED ${opsDate(p.submitted_at)} ${opsTime(p.submitted_at)}Z</div></div>`;
}
function openPirep(i) {
  const flight = loadActiveFlight(); if (!flight) return;
  flight.leg_state[i].pirep_open = true;
  recapTab = 'pirep';
  saveActiveFlight(flight);
  renderRecap(flight);
}
function sendToAircraftSoon(i) {
  const note = document.getElementById('sta-note-' + i);
  if (note) note.hidden = false;
}

const SIMBRIEF_NOTICE_DISMISSED_KEY = 'simdispatch_simbrief_aircraft_notice_dismissed';
let pendingPrepareLegIndex = null;

async function prepareLeg(index) {
  if (localStorage.getItem(SIMBRIEF_NOTICE_DISMISSED_KEY)) {
    await doPrepareLeg(index);
    return;
  }
  pendingPrepareLegIndex = index;
  document.getElementById('simbriefNoticeOverlay').classList.add('open');
}

function closeSimbriefNotice() {
  document.getElementById('simbriefNoticeOverlay').classList.remove('open');
  pendingPrepareLegIndex = null;
}

async function continueToSimbrief() {
  if (document.getElementById('simbriefNoticeDontShow').checked) {
    localStorage.setItem(SIMBRIEF_NOTICE_DISMISSED_KEY, '1');
  }
  const index = pendingPrepareLegIndex;
  document.getElementById('simbriefNoticeOverlay').classList.remove('open');
  pendingPrepareLegIndex = null;
  if (index != null) await doPrepareLeg(index);
}

async function doPrepareLeg(index) {
  const flight = loadActiveFlight(); if (!flight) return;
  const leg = flight.legs[index];
  const staticId = `simdispatch-${Date.now()}-${index}`;
  const delayMinutes = legExpectedDelayMinutes(leg);
  const qs = new URLSearchParams({
    flights: leg.flight_number,
    civalue: leg.cost_index,
    pax: leg.pax_count,
    aircraft_type: leg.aircraft_type || '',
    static_id: staticId,
    callsign: flight.callsigns ? flight.callsigns[index] : '',
    delay_minutes: delayMinutes,
    eobt: leg.eobt || '',
    taxi_out_minutes: leg.taxi_out_minutes
  });
  if (leg.tech) {
    if (leg.tech.max_fl) qs.set('max_fl', leg.tech.max_fl);
    if (leg.tech.extra_fuel_min) qs.set('extra_fuel_min', leg.tech.extra_fuel_min);
  }
  const remarks = simbriefRemarks(leg);
  if (remarks) qs.set('remarks', remarks);
  try {
    const resp = await fetch('/simbrief/redirect-url?' + qs.toString());
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    window.open(data.url, '_blank');
    flight.leg_state[index].simbrief = 'sent';
    flight.leg_state[index].simbrief_static_id = staticId;
    flight.leg_state[index].ofp = null;
    saveAndRender(flight);
  } catch (err) {
    alert('Could not build the SimBrief dispatch link. Check the connection and retry');
  }
}

async function fetchOfp(index) {
  const flight = loadActiveFlight(); if (!flight) return;
  const leg = flight.legs[index];
  const state = flight.leg_state[index];
  const username = getSimbriefUsername();
  if (!username) return;
  try {
    const resp = await fetch('/simbrief/ofp?username=' + encodeURIComponent(username));
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    if (state.simbrief_static_id && data.static_id && data.static_id !== state.simbrief_static_id) {
      alert('The most recent SimBrief OFP for this username doesn\'t match the dispatch just sent for this leg. Generate the OFP on SimBrief (in the tab that just opened), then fetch again.');
      return;
    }
    state.ofp = data;
    state.simbrief = 'available';
    state.aircraft_type_mismatch = await ofpAircraftTypeMismatch(leg, data);
    saveAndRender(flight);
  } catch (err) {
    alert('Could not fetch an OFP from SimBrief for that username. Make sure it\'s been generated there first.');
  }
}

async function ofpAircraftTypeMismatch(leg, ofp) {
  if (!leg.carrier || !leg.aircraft_type || !ofp.icao_type) return false;
  try {
    const carriers = await getCarriers();
    const carrier = carriers.find(c => c.icao === leg.carrier);
    const fleetEntry = carrier && carrier.fleet.find(ac => ac.type === leg.aircraft_type);
    return !!(fleetEntry && fleetEntry.simbrief_type !== ofp.icao_type);
  } catch (err) {
    return false;
  }
}
// "HH:MM" / "HH:MMZ" / "HH:MMZ (+1d)" -> minutes of day, or null.
function zuluMinutes(text) {
  const m = /^(\d{2}):(\d{2})/.exec(text || '');
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}
// A clock difference folded into -12h..+12h, so 23:50 -> 00:10 is +20.
function clockDiff(from, to) {
  return ((to - from + 720) % 1440 + 1440) % 1440 - 720;
}
// Shifts a "HH:MMZ (+Nd)" time by delta minutes, keeping the day suffix right.
function shiftZulu(text, delta) {
  const m = /^(\d{2}):(\d{2})Z?(?:\s*\(([+-]?\d+)d\))?/.exec(text || '');
  if (!m || !delta) return text;
  const total = Number(m[1]) * 60 + Number(m[2]) + (m[3] ? Number(m[3]) * 1440 : 0) + delta;
  const day = Math.floor(total / 1440), mins = total - day * 1440;
  const hhmm = String(Math.floor(mins / 60)).padStart(2, '0') + ':' + String(mins % 60).padStart(2, '0');
  return `${hhmm}Z${day ? ` (${day > 0 ? '+' : ''}${day}d)` : ''}`;
}

// Knock-on (reactionary, code 93) delay: once the previous leg is in, this
// leg can't leave before its actual in-block time plus the minimum
// turnaround. The leg's own delay runs in parallel with that wait, so the
// expected delay is the larger of the two, not their sum.
// `expected` marks a knock-on worked out from the previous leg's expected
// in-block time (dispatch, or an aircraft swap) rather than its actual one.
function applyReactionaryDelay(leg, prevAbit, expected = false) {
  const sobt = zuluMinutes(leg.sobt), abit = zuluMinutes(prevAbit);
  if (sobt == null || abit == null) return;
  const r = Math.max(0, clockDiff(sobt, abit + MIN_TURNAROUND_MINUTES));
  const before = legExpectedDelayMinutes(leg);
  leg.reactionary = r > 0 ? { iata_code: '93', description: 'Aircraft rotation: late arrival of aircraft from the previous sector', minutes: r, expected } : null;
  leg.expected_delay_minutes = Math.max(r, legOwnDelayMinutes(leg));
  const delta = leg.expected_delay_minutes - before;
  ['eobt', 'etot', 'eldt', 'eibt'].forEach(f => { if (leg[f]) leg[f] = shiftZulu(leg[f], delta); });
  revalidateSlot(leg);
}

// PIREP delay coding: like a real crew, the pilot codes a late departure
// (AOBT vs SOBT) with up to two IATA delay codes whose minutes add up to
// the delay. Prefilled from what the dispatch predicted.
function delayCodeOptions(selected) {
  let group = '', html = '<option value="">Choose a delay code</option>';
  IATA_DELAY_CODES.forEach(c => {
    if (c.group !== group) { html += `${group ? '</optgroup>' : ''}<optgroup label="${escAttr(c.group)}">`; group = c.group; }
    html += `<option value="${c.code}"${c.code === selected ? ' selected' : ''}>${c.code} ${escText(c.description)}</option>`;
  });
  return html + '</optgroup>';
}
function departureDelayMinutes(leg, aobt) {
  const sobt = zuluMinutes(leg.sobt), a = zuluMinutes(aobt);
  return sobt == null || a == null ? null : clockDiff(sobt, a);
}
function updateDelayCoding(i) {
  const flight = loadActiveFlight(); if (!flight) return;
  const leg = flight.legs[i];
  const box = document.getElementById('delaycoding-' + i);
  if (!box) return;
  const d = departureDelayMinutes(leg, readTimeField('aobt-' + i));
  if (d == null || d < DELAY_CODE_THRESHOLD_MINUTES) { box.classList.remove('open'); box.innerHTML = ''; box.dataset.delay = ''; return; }
  if (box.dataset.delay === String(d)) return;
  box.dataset.delay = String(d);
  const r = leg.reactionary ? Math.min(leg.reactionary.minutes, d) : 0;
  const rows = [];
  if (r) rows.push(['93', r]);
  if (d - r > 0) rows.push([leg.aircraft_change ? '46' : leg.delay ? leg.delay.iata_code : '', d - r]);
  while (rows.length < 2) rows.push(['', '']);
  box.innerHTML = `<div class="doc-sub">DEPARTURE DELAY ${d} MIN: CODE IT AS CREWS DO, UP TO TWO IATA CODES TOTALLING ${d} MIN</div>
    ${rows.map(([code, mins], k) => `<div class="doc-row delay-code-row">
      <label class="dk" for="dc-code-${i}-${k}">${k ? 'DELAY CODE 2' : 'DELAY CODE 1'}</label>
      <select id="dc-code-${i}-${k}" onchange="checkDelaySum(${i})" aria-label="${k ? 'Second delay code (optional)' : 'Main delay code'}">${delayCodeOptions(code)}</select>
      <span class="doc-time"><input id="dc-min-${i}-${k}" type="number" min="1" step="1" value="${mins}" oninput="checkDelaySum(${i})" aria-label="Minutes">MIN</span>
    </div>`).join('')}
    <p class="delay-sum" id="dc-sum-${i}"></p>`;
  box.classList.add('open');
  checkDelaySum(i);
}
function delayCodingRows(i) {
  return [0, 1].map(k => ({ code: (document.getElementById(`dc-code-${i}-${k}`) || {}).value || '',
    minutes: Number((document.getElementById(`dc-min-${i}-${k}`) || {}).value) })).filter(row => row.code);
}
function checkDelaySum(i) {
  const box = document.getElementById('delaycoding-' + i), out = document.getElementById('dc-sum-' + i);
  if (!box || !out) return;
  const d = Number(box.dataset.delay), sum = delayCodingRows(i).reduce((a, row) => a + (row.minutes || 0), 0);
  out.textContent = sum === d ? `CODED ${sum} OF ${d} MIN` : `CODED ${sum} OF ${d} MIN: THE MINUTES MUST ADD UP TO THE DELAY`;
  out.classList.toggle('bad', sum !== d);
}
function readDelayCoding(i, leg, aobt) {
  const d = departureDelayMinutes(leg, aobt);
  if (d == null || d < DELAY_CODE_THRESHOLD_MINUTES) return { codes: [] };
  const rows = delayCodingRows(i);
  if (!rows.length) return { error: `Code the ${d}-minute departure delay with at least one IATA delay code` };
  if (rows.some(row => !Number.isInteger(row.minutes) || row.minutes < 1)) return { error: 'Each delay code needs a whole number of minutes' };
  if (rows.reduce((a, row) => a + row.minutes, 0) !== d) return { error: `The delay-code minutes must add up to the ${d}-minute delay` };
  return { codes: rows.map(row => ({ code: row.code, minutes: row.minutes,
    description: (IATA_DELAY_CODES.find(c => c.code === row.code) || {}).description || '' })) };
}

function pirepDelayCodesText(pirep) {
  const codes = pirep && pirep.delay_codes;
  const slot = pirep && pirep.slot
    ? ` &middot; SLOT ${pirep.slot.met ? 'MET' : 'MISSED'} (${pirep.slot.delta >= 0 ? '+' : ''}${pirep.slot.delta} min)` : '';
  return (codes && codes.length ? ' &middot; DELAY ' + codes.map(c => `${escText(c.code)}/${c.minutes}`).join(' ') : '') + slot;
}
function readTimeField(id) {
  const hh = document.getElementById(id + '-hh').value;
  const mm = document.getElementById(id + '-mm').value;
  if (hh === '' || mm === '') return null;
  const h = Number(hh), m = Number(mm);
  if (!Number.isInteger(h) || h < 0 || h > 23 || !Number.isInteger(m) || m < 0 || m > 59) return null;
  return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
}

async function submitPirep(index) {
  const aobt = readTimeField('aobt-' + index);
  const atot = readTimeField('atot-' + index);
  const aldt = readTimeField('aldt-' + index);
  const abit = readTimeField('abit-' + index);
  const afad = Number(document.getElementById('afad-' + index).value);
  const normal = document.getElementById('normal-' + index).checked;
  if (!aobt || !atot || !aldt || !abit || !Number.isFinite(afad) || afad < 0 || !normal) {
    alert('Enter AOBT, ATOT, ALDT, AIBT (as HH and MM, 24-hour) and AFAD, then confirm the tech log entry');
    return;
  }
  const flight = loadActiveFlight(); if (!flight) return;
  const leg = flight.legs[index];
  const state = flight.leg_state[index];
  const coded = readDelayCoding(index, leg, aobt);
  if (coded.error) { alert(coded.error); return; }
  const pirep = {aobt, atot, aldt, abit, afad, no_new_mel_or_non_normal: true, submitted_at: new Date().toISOString()};
  const remarks = (document.getElementById('remarks-' + index) || {}).value || '';
  if (remarks.trim()) pirep.remarks = remarks.trim().slice(0, 500);
  if (coded.codes.length) pirep.delay_codes = coded.codes;
  if (leg.atfm) {
    const delta = clockDiff(zuluMinutes(leg.atfm.ctot), zuluMinutes(atot));
    const [lo, hi] = leg.atfm.window || [-5, 10];
    pirep.slot = { ctot: leg.atfm.ctot, delta, met: delta >= lo && delta <= hi };
  }
  state.pirep = pirep;
  flight.leg_status[index].status = 'done';
  if (index + 1 < flight.legs.length) applyReactionaryDelay(flight.legs[index + 1], abit);
  saveAndRender(flight);

  try {
    await fetch('/pireps', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        flight_number: leg.flight_number,
        callsign: flight.callsigns ? flight.callsigns[index] : '',
        departure_icao: leg.departure_info.icao,
        arrival_icao: leg.arrival_info.icao,
        leg: leg,
        ofp: state.ofp,
        loadsheet: state.loadsheet,
        pirep: pirep
      })
    });
  } catch (err) {
    // The leg is already closed out locally either way - the server-side
    // PIREP log is a convenience list, not required for the app to work.
  }
}
function skipFlight() {
  if (!confirm('Skip this active flight? This discards the unfinished dispatch and PIREP data')) return;
  clearActiveFlight(); renderSearchUI();
}

function goHome() {
  renderSearchUI();
}

// Marks which header button matches the page on screen: 'search',
// 'flight' or 'profile' (profile covers account settings too).
let currentAppSection = '';
function setAppNav(section) {
  currentAppSection = section;
  [['search', 'navSearch'], ['flight', 'activeFlightBtn'], ['profile', 'navProfile']].forEach(([key, id]) => {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.classList.toggle('is-current', key === section);
    if (key === section) btn.setAttribute('aria-current', 'page'); else btn.removeAttribute('aria-current');
  });
  document.body.classList.toggle('page-search', section === 'search');
  setPageFit(false);
}

// Desktop pages that fit the screen without page scrolling (see the
// page-fit CSS); each page opts in after setAppNav.
function setPageFit(on) {
  document.body.classList.toggle('page-fit', on);
  if (on) sizeSearchLayout();
}

function goActiveFlight() {
  const flight = loadActiveFlight();
  if (!flight) { alert('No active flight yet. Search and confirm one first'); return; }
  renderRecap(flight);
}

function calcAge(birthDateStr) {
  if (!birthDateStr) return null;
  const dob = new Date(birthDateStr);
  if (isNaN(dob)) return null;
  const now = new Date();
  let age = now.getFullYear() - dob.getFullYear();
  const m = now.getMonth() - dob.getMonth();
  if (m < 0 || (m === 0 && now.getDate() < dob.getDate())) age--;
  return age;
}

// Storage stays ISO (YYYY-MM-DD, sorts naturally, unambiguous) - only
// the display format is DD/MM/YYYY, per request. These two functions
// are the only place that conversion happens.
function isoToDisplayDate(iso) {
  if (!iso) return '';
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : '';
}
function displayDateToIso(display) {
  const m = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec((display || '').trim());
  if (!m) return null;
  const [, dd, mm, yyyy] = m;
  const d = new Date(Number(yyyy), Number(mm) - 1, Number(dd));
  if (d.getFullYear() !== Number(yyyy) || d.getMonth() !== Number(mm) - 1 || d.getDate() !== Number(dd)) return null;
  return `${yyyy}-${mm}-${dd}`;
}

const SIMULATORS = ['MSFS2024', 'MSFS', 'XPlane 12', 'XPlane 11', 'P3D', 'Others'];

function avatarHtml(profile, size) {
  size = size || 64;
  if (profile.photo_url) {
    return `<img src="${escAttr(profile.photo_url)}" alt="" style="width:${size}px; height:${size}px; border-radius:50%; object-fit:cover; flex-shrink:0;">`;
  }
  const initial = ((profile.username || profile.first_name || '?').trim().charAt(0) || '?').toUpperCase();
  return `<div style="width:${size}px; height:${size}px; border-radius:50%; background:var(--titlebar); color:#fff; display:flex; align-items:center; justify-content:center; font-size:${Math.round(size * 0.4)}px; font-weight:700; flex-shrink:0;">${initial}</div>`;
}

async function renderUserPage() {
  setAppNav('profile');
  document.getElementById('mainContent').innerHTML = `<div class="panel"><div class="panel-body"><div class="empty-state">LOADING...</div></div></div>`;

  let settings = null, stats = null;
  try {
    const [settingsResp, logResp] = await Promise.all([fetch('/settings'), fetch('/stats/flights')]);
    settings = await settingsResp.json();
    statsData = await logResp.json();
    stats = profileStatsFromLogbook(statsData);
  } catch (err) {
    document.getElementById('mainContent').innerHTML = `<div class="panel"><div class="empty-state">COULD NOT LOAD YOUR PROFILE. CHECK THE CONNECTION AND RETRY</div></div>`;
    return;
  }
  currentSettings = settings;
  const p = settings.profile;
  const age = calcAge(p.birth_date);
  const fullName = [p.first_name, p.last_name].filter(Boolean).join(' ');
  const dobLine = p.birth_date ? `DoB: ${isoToDisplayDate(p.birth_date)}${age != null ? ` (${age}yrs)` : ''}` : '';

  setPageFit(true);
  document.getElementById('mainContent').innerHTML = `
    <div class="profile-layout">
    <div class="profile-col">
    <div class="panel profile-panel">
      <div class="panel-header">
        PILOT PROFILE
        <button class="action small" onclick="renderAccountSettings()">ACCOUNT SETTINGS</button>
      </div>
      <div class="panel-body" style="display:flex; gap:16px; align-items:center; flex-wrap:wrap;">
        ${avatarHtml(p, 96)}
        <div>
          <div style="font-size:16px; font-weight:700;">${p.username ? escAttr(p.username) : 'NO USERNAME SET'}</div>
          ${fullName ? `<div class="placeholder-text">${escAttr(fullName)}</div>` : ''}
          ${dobLine ? `<div class="placeholder-text">${dobLine}</div>` : ''}
          ${p.nationality ? `<div class="placeholder-text">${escAttr(p.nationality)}</div>` : ''}
          ${p.preferred_base ? `<div class="placeholder-text">Base: ${escAttr(p.preferred_base)}</div>` : ''}
        </div>
      </div>
    </div>

    <div class="panel destinations-panel">
      <div class="panel-header">DESTINATIONS REACHED</div>
      <div class="panel-body">
        <div id="destinationsMap" class="route-map destinations-map"></div>
        <p class="placeholder-text" style="margin-top:6px;">Base airport shown in red</p>
      </div>
    </div>
    </div>

    <div class="profile-col">
    <div class="panel stats-panel">
      <div class="panel-header">
        FLYING STATS
        <button class="action small" type="button" onclick="statsState = null; renderStatsPage()">FULL STATS</button>
      </div>
      <div class="panel-body">
        ${profileStatsSummary()}
        <div id="monthlyChart" style="margin-top:10px;"></div>
      </div>
    </div>

    <div class="panel pirep-log-panel">
      <div class="panel-header">PIREP LOG</div>
      <div class="panel-body" id="pirepLogBody"><div class="empty-state">LOADING...</div></div>
    </div>
    </div>
    </div>`;

  renderMonthlyChart(stats.monthly || []);
  initDestinationsMap('destinationsMap', stats.airports || [], p.preferred_base);
  loadPirepLog();
}

// All-time headline figures for the profile (same definitions as the
// Stats page: flight time is off-block to in-block).
function profileStatsSummary() {
  const rows = (statsData && statsData.flights) || [];
  const k = statsKpis(rows);
  const since = addDays(todayIso(), -29);
  const recent = rows.filter(r => r.date && r.date >= since).length;
  const cell = (label, value) => `<div><span>${label}</span><b>${value}</b></div>`;
  return `<div class="profile-stats-kpis">
    ${cell('Flights', fmtNum(k.flights))}${cell('Flight time', fmtHours(k.ft))}${cell('Distance', fmtNum(Math.round(k.dist)) + ' nm')}
    ${cell('Airports', fmtNum(k.airports))}${cell('Countries', fmtNum(k.countries))}${cell('Last 30 days', fmtNum(recent) + (recent === 1 ? ' flight' : ' flights'))}
  </div>`;
}

// The profile's monthly chart and destinations map, built from the same
// logbook rows the Stats page uses (one request instead of two).
function profileStatsFromLogbook(data) {
  const flights = (data && data.flights) || [], info = (data && data.airports) || {};
  const months = new Map(), visits = new Map();
  flights.forEach(f => {
    if (f.date) months.set(f.date.slice(0, 7), (months.get(f.date.slice(0, 7)) || 0) + 1);
    [f.dep, f.arr].forEach(icao => { if (icao) visits.set(icao, (visits.get(icao) || 0) + 1); });
  });
  const airports = [...visits.entries()].sort().map(([icao, count]) => ({ icao, count, ...(info[icao] || {}) }));
  const base = data && data.base;
  if (base && base.icao && !visits.has(base.icao)) airports.push({ ...base, count: 0 });
  return { monthly: [...months.entries()].sort().map(([month, n]) => ({ month, flights: n })), airports };
}

function renderMonthlyChart(monthly) {
  const el = document.getElementById('monthlyChart');
  if (!el) return;
  if (!monthly.length) { el.innerHTML = '<div class="empty-state">No flights logged yet</div>'; return; }
  const recent = monthly.slice(-12);
  const max = Math.max(...recent.map(m => m.flights), 1);
  const bars = recent.map(m => {
    const pct = Math.max(4, Math.round((m.flights / max) * 100));
    const [year, mo] = m.month.split('-');
    const label = new Date(Number(year), Number(mo) - 1, 1).toLocaleDateString(undefined, { month: 'short' });
    return `
      <div style="display:flex; flex-direction:column; align-items:center; gap:4px; flex:1;">
        <div style="font-size:11px; color:var(--text-muted);">${m.flights}</div>
        <div style="width:100%; max-width:28px; height:80px; display:flex; align-items:flex-end; background:var(--face-dark); border-radius:3px;" title="${m.month}">
          <div style="width:100%; height:${pct}%; background:var(--titlebar); border-radius:3px 3px 0 0;"></div>
        </div>
        <div style="font-size:10.5px; color:var(--text-muted);">${label}</div>
      </div>`;
  }).join('');
  el.innerHTML = `<div style="display:flex; gap:6px; align-items:flex-end;">${bars}</div>`;
}

function initDestinationsMap(id, airports, baseIcao) {
  const el = document.getElementById(id);
  if (!el || typeof L === 'undefined') return;
  const plottable = airports.filter(a => a.lat != null && a.lon != null);
  const map = L.map(id, { zoomControl: true, scrollWheelZoom: false, maxZoom: BASEMAP_MAX_ZOOM });
  addBasemap(map);
  if (!plottable.length) { map.setView([20, 0], 2); return; }
  const points = [];
  plottable.forEach(a => {
    points.push([a.lat, a.lon]);
    const isBase = baseIcao && a.icao === baseIcao;
    const marker = L.circleMarker([a.lat, a.lon], {
      radius: isBase ? 8 : 6,
      color: isBase ? '#B42318' : '#2563EB',
      fillColor: isBase ? '#B42318' : '#2563EB',
      fillOpacity: 0.85, weight: 2,
    }).addTo(map);
    marker.bindTooltip(`${a.icao}${a.name ? ' — ' + a.name : ''} (${a.count} visit${a.count === 1 ? '' : 's'})${isBase ? ' — BASE' : ''}`);
  });
  map.fitBounds(L.latLngBounds(points), { padding: [30, 30] });
}

async function renderAccountSettings() {
  setAppNav('profile');
  document.getElementById('mainContent').innerHTML = `<div class="panel"><div class="panel-body"><div class="empty-state">LOADING...</div></div></div>`;

  let settings = null, genOptions = null, carriers = null;
  try {
    const [settingsResp, optionsResp, carriersResp] = await Promise.all([fetch('/settings'), fetch('/generation-options'), fetch('/carriers')]);
    settings = await settingsResp.json();
    genOptions = await optionsResp.json();
    carriers = await carriersResp.json();
  } catch (err) {
    document.getElementById('mainContent').innerHTML = `<div class="panel"><div class="empty-state">COULD NOT LOAD ACCOUNT SETTINGS. CHECK THE CONNECTION AND RETRY</div></div>`;
    return;
  }
  currentSettings = settings;
  const p = settings.profile;
  const simbriefUsername = localStorage.getItem(SIMBRIEF_USERNAME_KEY) || '';
  const countryOptions = COUNTRIES.map(c => `<option value="${escAttr(c)}" ${p.nationality === c ? 'selected' : ''}>${c}</option>`).join('');
  const simulatorOptions = SIMULATORS.map(s => `<option value="${escAttr(s)}" ${p.simulator === s ? 'selected' : ''}>${s}</option>`).join('');
  const ownedAircraft = new Set(p.aircraft_owned || []);
  const aircraftGroupsHtml = carriers.map(c => `
    <div class="gen-items-group-label">${escAttr(c.name.toUpperCase())}</div>
    ${c.fleet.map(ac => `
      <label class="checkbox-row"><input type="checkbox" class="aircraft-owned-item" value="${escAttr(ac.type)}" ${ownedAircraft.has(ac.type) ? 'checked' : ''}> ${escAttr(ac.type)} <span class="hint">(${ac.seats} seats)</span></label>
    `).join('')}
  `).join('');

  document.getElementById('mainContent').innerHTML = `
    <div class="panel">
      <div class="panel-header">
        ACCOUNT SETTINGS
        <button class="action small" onclick="renderUserPage()">BACK TO PROFILE</button>
      </div>
      <div class="panel-body">
        <div style="display:flex; align-items:center; gap:16px; margin-bottom:18px;">
          <div id="acctPhotoPreview">${avatarHtml(p, 72)}</div>
          <div>
            <button class="action small" type="button" onclick="document.getElementById('acctPhotoInput').click()">CHANGE PHOTO</button>
            <input type="file" id="acctPhotoInput" accept="image/jpeg,image/png,image/webp" style="display:none;" onchange="uploadAccountPhoto()">
            <div class="field-note" id="acctPhotoStatus">JPEG, PNG or WebP, up to 2MB</div>
          </div>
        </div>
        <div class="params-grid">
          <div class="field"><label for="acctEmail">EMAIL</label><input type="text" id="acctEmail" value="${escAttr(CURRENT_USER_EMAIL)}" disabled></div>
          <div class="field"><label for="acctUsername">USERNAME</label><input type="text" id="acctUsername" value="${escAttr(p.username)}" disabled></div>
          <div class="field"><label for="profileFirstName">FIRST NAME</label><input type="text" id="profileFirstName" autocomplete="off" value="${escAttr(p.first_name)}"></div>
          <div class="field"><label for="profileLastName">LAST NAME</label><input type="text" id="profileLastName" autocomplete="off" value="${escAttr(p.last_name)}"></div>
          <div class="field">
            <label for="profileBirthDate">BIRTH DATE <span class="hint">(DD/MM/YYYY, optional)</span></label>
            <input type="text" id="profileBirthDate" autocomplete="off" placeholder="DD/MM/YYYY" maxlength="10" value="${escAttr(isoToDisplayDate(p.birth_date))}">
            <div class="field-error" id="profileBirthDateError">ENTER A VALID DATE AS DD/MM/YYYY</div>
          </div>
          <div class="field">
            <label for="profileNationality">NATIONALITY <span class="hint">(optional)</span></label>
            <select id="profileNationality"><option value="">&mdash;</option>${countryOptions}</select>
          </div>
          <div class="field">
            <label for="profilePreferredBase">PREFERRED BASE <span class="hint">(optional, ICAO/IATA)</span></label>
            <input type="text" id="profilePreferredBase" autocomplete="off" maxlength="4" style="text-transform:uppercase;" value="${escAttr(p.preferred_base)}">
            <div class="custom-dropdown" id="profilePreferredBaseDropdown"></div>
          </div>
          <div class="field">
            <label for="profileSimulator">SIMULATOR <span class="hint">(optional)</span></label>
            <select id="profileSimulator"><option value="">&mdash;</option>${simulatorOptions}</select>
          </div>
          <div class="field">
            <label for="userSimbriefUsername">SIMBRIEF USERNAME</label>
            <input type="text" id="userSimbriefUsername" autocomplete="off" value="${escAttr(simbriefUsername)}" placeholder="e.g. jdoe123">
            <div class="field-error" id="userSimbriefUsernameError">3-30 CHARACTERS: LETTERS, NUMBERS, UNDERSCORE OR HYPHEN ONLY</div>
          </div>
        </div>
        <p class="placeholder-text">Email and username can't be changed here yet</p>
        <div style="margin-top:10px; display:flex; align-items:center; gap:10px;">
          <button class="action" onclick="savePilotProfile()">SAVE</button>
          <span class="placeholder-text" id="profileSaveStatus"></span>
        </div>
        <div style="margin-top:16px; padding-top:16px; border-top:1px solid var(--shadow-md); display:flex; align-items:center; gap:10px;">
          <button class="action small" type="button" onclick="sendPasswordReset()">CHANGE PASSWORD</button>
          <span class="placeholder-text" id="passwordResetStatus"></span>
        </div>
      </div>
    </div>

    <div class="panel" style="margin-top:14px;">
      <div class="panel-header">AIRCRAFT YOU FLY</div>
      <div class="panel-body">
        <p class="placeholder-text">Tell us which aircraft you actually fly/have installed in the simulator - it affects which aircraft gets assigned to your flights (a carrier that flies more than one type, like easyJet, picks between your selections; leave all unchecked for no preference). You can change this any time</p>
        <div class="gen-items-list" style="max-height:none;">${aircraftGroupsHtml}</div>
        <div style="margin-top:10px; display:flex; align-items:center; gap:10px;">
          <button class="action" onclick="savePilotProfile()">SAVE</button>
          <span class="placeholder-text" id="aircraftOwnedSaveStatus"></span>
        </div>
      </div>
    </div>

    <div class="panel" style="margin-top:14px;">
      <div class="panel-header">DISPATCH COMPLICATIONS</div>
      <div class="panel-body">
        ${renderComplicationCategory('delay', 'DELAYS', "Delays before the first sector of a trip, in realistic proportions. Later sectors only inherit a late inbound aircraft. Times and the SimBrief plan update automatically", genOptions.delay, settings.generation.delay, delaySettingsRow, d => d.code, null, genOptions.default_probability.delay, 'Chance of a delay on the first sector')}
        ${renderComplicationCategory('lmc', 'LAST-MINUTE CHANGES (LMC)', 'Late changes to passenger or baggage figures after the loadsheet is prepared, to be amended before sign-off', genOptions.lmc, settings.generation.lmc, lmcSettingsRow, l => l.id, null, genOptions.default_probability.lmc, 'Chance per trip')}
        ${renderComplicationCategory('mel', 'DEFERRED DEFECTS (MEL)', "Equipment the aircraft may be dispatched without, under conditions. Up to three per trip: some limit the cruise level, add fuel, require ground equipment or rule out certain weather", genOptions.mel, settings.generation.mel, melSettingsRow, m => m.id, m => m.fleet, genOptions.default_probability.mel, 'Chance of at least one deferred defect')}
        ${renderComplicationCategory('cdl', 'MISSING PANELS (CDL)', 'An external panel or seal missing from the airframe: adds a small fuel penalty and can reduce the weight limits', genOptions.cdl || [], settings.generation.cdl || {enabled: true, disabled_ids: []}, cdlSettingsRow, c => c.id, c => c.fleet, genOptions.default_probability.cdl, 'Chance per trip')}
        <div style="margin-top:10px; display:flex; align-items:center; gap:10px;">
          <button class="action" onclick="saveGenerationSettings()">SAVE</button>
          <span class="placeholder-text" id="generationSaveStatus"></span>
        </div>
      </div>
    </div>

    <div style="margin-top:14px; text-align:right;">
      <button class="action danger" type="button" onclick="renderDeleteAccountPage()">DELETE ACCOUNT</button>
    </div>`;

  attachAirportDropdown('profilePreferredBase', 'profilePreferredBaseDropdown', '/airports/search/world');
}

function renderDeleteAccountPage() {
  setAppNav('profile');
  document.getElementById('mainContent').innerHTML = `
    <div class="panel">
      <div class="panel-header">
        DELETE ACCOUNT
        <button class="action small" onclick="renderAccountSettings()">CANCEL</button>
      </div>
      <div class="panel-body">
        <div id="deleteAccountStep1">
          <p class="placeholder-text">This permanently deletes your account and every PIREP, stat and setting attached to it. This cannot be undone and this data cannot be recovered</p>
          <div class="field" style="max-width:260px; margin-bottom:16px;">
            <label for="deleteConfirmText">Type DELETE to confirm</label>
            <input type="text" id="deleteConfirmText" autocomplete="off">
          </div>
          <button class="action danger" type="button" onclick="proceedToDeleteStep2()">CONTINUE</button>
        </div>
        <div id="deleteAccountStep2" style="display:none;">
          <p class="placeholder-text">Enter your password to permanently delete your account</p>
          <div class="field" style="max-width:260px; margin-bottom:16px;">
            <label for="deletePassword">Password</label>
            <input type="password" id="deletePassword" autocomplete="off">
          </div>
          <div class="notice-banner" id="deleteError" style="display:none; margin-bottom:16px;"></div>
          <button class="action danger" type="button" onclick="confirmDeleteAccount()">PERMANENTLY DELETE ACCOUNT</button>
          <button class="action small" type="button" onclick="renderAccountSettings()">CANCEL</button>
        </div>
      </div>
    </div>`;
}

function proceedToDeleteStep2() {
  if (document.getElementById('deleteConfirmText').value.trim() !== 'DELETE') {
    alert('Type DELETE exactly (all capitals) to confirm');
    return;
  }
  document.getElementById('deleteAccountStep1').style.display = 'none';
  document.getElementById('deleteAccountStep2').style.display = 'block';
}

async function confirmDeleteAccount() {
  const password = document.getElementById('deletePassword').value;
  const errEl = document.getElementById('deleteError');
  errEl.style.display = 'none';
  if (!password) { errEl.textContent = 'Enter your password'; errEl.style.display = 'block'; return; }
  if (!confirm('This is final and cannot be undone. Permanently delete your account?')) return;
  try {
    const resp = await fetch('/account/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'password=' + encodeURIComponent(password),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'Could not delete account');
    localStorage.clear();
    window.location.href = '/';
  } catch (err) {
    errEl.textContent = err.message;
    errEl.style.display = 'block';
  }
}

async function uploadAccountPhoto() {
  const input = document.getElementById('acctPhotoInput');
  const file = input.files[0];
  if (!file) return;
  const status = document.getElementById('acctPhotoStatus');
  status.textContent = 'UPLOADING...';
  const formData = new FormData();
  formData.append('photo', file);
  try {
    const resp = await fetch('/account/photo', { method: 'POST', body: formData });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'Upload failed');
    currentSettings = data;
    document.getElementById('acctPhotoPreview').innerHTML = avatarHtml(data.profile, 72);
    status.textContent = 'Photo updated';
  } catch (err) {
    status.textContent = err.message;
  } finally {
    input.value = '';
  }
}

async function sendPasswordReset() {
  const status = document.getElementById('passwordResetStatus');
  status.textContent = 'SENDING...';
  try {
    const resp = await fetch('/auth/reset', {
      method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'email=' + encodeURIComponent(CURRENT_USER_EMAIL),
    });
    if (!resp.ok) throw new Error('Could not send reset email');
    status.textContent = `Check ${CURRENT_USER_EMAIL} for a reset link`;
  } catch (err) {
    status.textContent = err.message;
  }
}

const FLEET_LABELS = { '738': '737-800', 'a320fam': 'A320 FAMILY' };

// One settings block per category: on/off switch, a probability slider
// (realistic default or the pilot's own chance), and a table of every item
// with what it does and what it asks of the crew, each switchable.
// rowFn(item) -> {item, description, effect, actions: []} (HTML-escaped).
function renderComplicationCategory(key, title, explanation, items, categorySettings, rowFn, idFn, groupFn, realisticPct, chanceLabel) {
  const disabledIds = new Set(categorySettings.disabled_codes || categorySettings.disabled_ids || []);
  const masterOn = categorySettings.enabled !== false;
  const custom = categorySettings.probability != null;
  const pct = custom ? categorySettings.probability : realisticPct;
  const row = item => {
    const id = idFn(item), on = !disabledIds.has(id), r = rowFn(item);
    return `<tr class="${on ? '' : 'off'}">
      <td><input type="checkbox" class="gen-item" data-id="${escAttr(id)}" ${on ? 'checked' : ''} ${masterOn ? '' : 'disabled'}
        onchange="this.closest('tr').classList.toggle('off', !this.checked)" aria-label="Include ${escAttr(id)}"></td>
      <td class="item" data-label="ITEM">${r.item}</td>
      <td data-label="DESCRIPTION">${r.description}</td>
      <td class="effect" data-label="EFFECT ON THE FLIGHT">${r.effect}</td>
      <td class="actions" data-label="CREW ACTIONS">${r.actions.length ? `<ul>${r.actions.map(a => `<li>${a}</li>`).join('')}</ul>` : '&mdash;'}</td>
    </tr>`;
  };
  let rows;
  if (groupFn) {
    // Items sharing a fleet get their own sub-heading: with two families
    // active, "Autopilot" alone is ambiguous between a 737 and an A320 item.
    const groups = new Map();
    items.forEach(item => { const g = groupFn(item); if (!groups.has(g)) groups.set(g, []); groups.get(g).push(item); });
    rows = Array.from(groups.entries()).map(([g, list]) =>
      `<tr class="gen-group"><td colspan="5">${FLEET_LABELS[g] || escText(g)}</td></tr>${list.map(row).join('')}`).join('');
  } else {
    rows = items.map(row).join('');
  }
  return `
    <div class="leg-block" style="margin-bottom:12px;">
      <div class="leg-block-header">
        <div class="switch-row">
          <span class="switch-toggle">
            <input type="checkbox" id="gen-${key}-master" onchange="onCategoryMasterToggle('${key}')" ${masterOn ? 'checked' : ''}>
            <span class="track"></span>
          </span>
          <span>${title}</span>
        </div>
      </div>
      <div class="leg-block-body">
        <p class="placeholder-text">${explanation}</p>
        <div class="prob-row">
          <label for="gen-${key}-prob">${chanceLabel}</label>
          <input type="range" id="gen-${key}-prob" min="0" max="100" step="1" value="${pct}" data-custom="${custom ? 1 : 0}"
            data-realistic="${realisticPct}" oninput="onProbabilityInput('${key}')" ${masterOn ? '' : 'disabled'}>
          <output id="gen-${key}-prob-val">${probabilityLabel(pct, custom)}</output>
          <button class="ghost small" type="button" onclick="resetProbability('${key}')" ${masterOn ? '' : 'disabled'}>REALISTIC</button>
        </div>
        <details>
          <summary>SHOW ALL ${items.length} ITEMS</summary>
          <div class="gen-table-wrap"><table class="gen-table" id="gen-${key}-items">
            <thead><tr><th>ON</th><th>ITEM</th><th>DESCRIPTION</th><th>EFFECT ON THE FLIGHT</th><th>CREW ACTIONS</th></tr></thead>
            <tbody>${rows}</tbody>
          </table></div>
        </details>
      </div>
    </div>`;
}

function probabilityLabel(pct, custom) {
  return `${pct}%<small>${custom ? 'your setting' : 'realistic'}</small>`;
}
function onProbabilityInput(key) {
  const input = document.getElementById(`gen-${key}-prob`);
  input.dataset.custom = '1';
  document.getElementById(`gen-${key}-prob-val`).innerHTML = probabilityLabel(input.value, true);
}
function resetProbability(key) {
  const input = document.getElementById(`gen-${key}-prob`);
  input.value = input.dataset.realistic;
  input.dataset.custom = '0';
  document.getElementById(`gen-${key}-prob-val`).innerHTML = probabilityLabel(input.value, false);
}

// ---- Settings table rows (plain wording) ----
const NO_WX_LABELS = { TS: 'thunderstorms', ICING: 'icing', LVP: 'low visibility (below CAT I) at the destination', CONTAM: 'a contaminated runway', PRECIP: 'rain or snow' };
function delaySettingsRow(d) {
  const [lo, hi] = d.duration_range_minutes || [0, 0];
  const typical = Math.round(lo + (hi - lo) / 3);
  const effect = [`Leaves ${lo} to ${hi} min late, usually about ${typical} min`];
  if (d.atfm) effect.push('Comes with a take-off slot from air traffic control');
  if (d.code === '77') effect.push('Only when a thunderstorm is reported at the departure airport, and then more likely than not');
  else if (d.weather_gated) effect.push('Only happens when the weather is actually bad');
  if (d.code === '83') effect.push('More likely when thunderstorms are reported at the destination');
  if (d.code === '93') effect.push('First flight of a trip only');
  const actions = ['Your times and SimBrief plan update automatically', 'If you leave late, pick the reason in your flight report'];
  if (d.atfm) actions.unshift('Take off between 5 min before and 10 min after your slot');
  if (d.code === '75') actions.unshift('Allow time for de-icing before departure');
  if (d.code === '71' || d.code === '72') actions.unshift(`Check the ${d.code === '71' ? 'departure' : 'destination'} weather before planning`);
  return { item: `<b>${escText(d.plain || d.description)}</b><span class="tech-tag">CODE ${escText(d.code)}</span>`,
    description: escText(d.plain ? `Delay reason: ${d.plain.charAt(0).toLowerCase()}${d.plain.slice(1)}` : d.description),
    effect: effect.join('<br>'), actions };
}
function lmcSettingsRow(l) {
  const [a, b] = l.delta_range || [0, 0];
  const fmt = n => (n > 0 ? '+' : n < 0 ? '&minus;' : '') + Math.abs(n);
  const range = a === b ? fmt(a) : `${fmt(a)} to ${fmt(b)}`;
  const pax = l.type === 'pax_change';
  const when = l.needs_full_flight ? '<br>Only on a full flight' : l.needs_free_seats ? '<br>Only when seats are free' : '';
  return {
    item: `<b>${pax ? 'Passengers' : 'Bags and cargo'}</b>`, description: escText(l.description),
    effect: `${range} ${escText(l.unit)} after the loadsheet is ready${when}`,
    actions: l.delta_range[1] > 0 ? ['Accept or decline it on the loadsheet before you sign', 'Extra load over the weight limits can only be declined']
                 : ['Acknowledge it on the loadsheet: offloaded passengers and bags always come off'],
  };
}
function melEffectLines(e) {
  const out = [];
  if (e.max_fl) out.push(`Cruise no higher than FL${e.max_fl} (set in SimBrief)`);
  if (e.extra_fuel_min) out.push(`${e.extra_fuel_min} min of extra fuel (added in SimBrief)`);
  if (e.min_main_tank_fuel_kg) out.push(`Each wing tank needs at least ${fmtNum(e.min_main_tank_fuel_kg.takeoff)} kg at take-off and ${fmtNum(e.min_main_tank_fuel_kg.landing)} kg at landing`);
  if (e.centre_tank_empty) out.push(`Centre tank can't be used${e.max_fuel_kg ? ` (about ${fmtNum(e.max_fuel_kg)} kg of fuel at most)` : ''}`);
  if (e.ground_support && e.ground_support.length) out.push(`Requires ${e.ground_support.map(g => g === 'GPU' ? 'a ground power unit (GPU)' : 'an air start unit (ASU)').join(' and ')} on every stand`);
  if (e.no_wx && e.no_wx.length) out.push(`Can't fly into ${e.no_wx.map(c => NO_WX_LABELS[c] || c).join(', ')}`);
  if (e.performance) out.push('Needs more runway for take-off and landing');
  if (e.cargo_hold_empty) out.push('One cargo hold must stay empty');
  return out;
}
function melSettingsRow(m) {
  const out = melEffectLines(m.effects || {});
  if (!out.length) out.push('No effect on how you fly');
  return {
    item: `<b>${escText(m.name || m.system)}</b>`,
    description: escText(m.plain || m.description),
    effect: out.join('<br>'),
    actions: (m.steps || (m.procedures || []).map(stripMO)).map(escText),
  };
}
function cdlSettingsRow(c) {
  const e = c.effects || {}, out = [];
  if (e.fuel_burn_pct) out.push(`Burns ${e.fuel_burn_pct}% more fuel`);
  if (e.weight_penalty_kg) out.push(`Maximum weights ${e.weight_penalty_kg} kg lower`);
  if (!out.length) out.push('No effect');
  return { item: `<b>${escText(c.name || c.part)}</b>`, description: escText(c.plain || c.description), effect: out.join('<br>'),
    actions: (c.steps || (c.note ? [c.note] : [])).map(escText) };
}

function onCategoryMasterToggle(key) {
  const masterOn = document.getElementById(`gen-${key}-master`).checked;
  document.querySelectorAll(`#gen-${key}-items input[type=checkbox]`).forEach(cb => { cb.disabled = !masterOn; });
  const prob = document.getElementById(`gen-${key}-prob`);
  if (prob) { prob.disabled = !masterOn; prob.parentElement.querySelector('button').disabled = !masterOn; }
}

function readCategorySettings(key, disabledFieldName) {
  const enabled = document.getElementById(`gen-${key}-master`).checked;
  const disabled = Array.from(document.querySelectorAll(`#gen-${key}-items input[type=checkbox]`))
    .filter(cb => !cb.checked)
    .map(cb => cb.dataset.id);
  const prob = document.getElementById(`gen-${key}-prob`);
  const probability = prob && prob.dataset.custom === '1' ? Number(prob.value) : null;
  return { enabled, [disabledFieldName]: disabled, probability };
}

async function saveGenerationSettings() {
  const payload = {
    profile: currentSettings.profile,
    generation: {
      delay: readCategorySettings('delay', 'disabled_codes'),
      lmc: readCategorySettings('lmc', 'disabled_ids'),
      mel: readCategorySettings('mel', 'disabled_ids'),
      cdl: readCategorySettings('cdl', 'disabled_ids'),
    }
  };
  try {
    const resp = await fetch('/settings', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
    if (!resp.ok) throw new Error('save failed');
    currentSettings = await resp.json();
    document.getElementById('generationSaveStatus').textContent = 'SAVED.';
  } catch (err) {
    alert('Could not save these settings. Check the connection (and that a database is configured) and retry');
  }
}

async function savePilotProfile() {
  const usernameInput = document.getElementById('userSimbriefUsername');
  const usernameError = document.getElementById('userSimbriefUsernameError');
  const username = usernameInput.value.trim();
  const usernameValid = username === '' || /^[A-Za-z0-9_-]{3,30}$/.test(username);
  usernameError.classList.toggle('show', !usernameValid);

  const birthDateInput = document.getElementById('profileBirthDate');
  const birthDateError = document.getElementById('profileBirthDateError');
  const birthDateRaw = birthDateInput.value.trim();
  const birthDateIso = birthDateRaw ? displayDateToIso(birthDateRaw) : '';
  const birthDateValid = birthDateRaw === '' || birthDateIso !== null;
  birthDateError.classList.toggle('show', !birthDateValid);

  if (!usernameValid || !birthDateValid) return;

  const aircraftOwned = Array.from(document.querySelectorAll('.aircraft-owned-item:checked')).map(cb => cb.value);

  const profile = {
    ...currentSettings.profile,
    first_name: document.getElementById('profileFirstName').value.trim(),
    last_name: document.getElementById('profileLastName').value.trim(),
    birth_date: birthDateIso || '',
    nationality: document.getElementById('profileNationality').value,
    preferred_base: document.getElementById('profilePreferredBase').value.trim().toUpperCase(),
    simulator: document.getElementById('profileSimulator').value,
    aircraft_owned: aircraftOwned,
  };
  const payload = { profile, generation: currentSettings.generation };
  try {
    const resp = await fetch('/settings', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
    if (!resp.ok) throw new Error('save failed');
    currentSettings = await resp.json();
    if (username) localStorage.setItem(SIMBRIEF_USERNAME_KEY, username);
    else localStorage.removeItem(SIMBRIEF_USERNAME_KEY);
    document.getElementById('profileSaveStatus').textContent = 'SAVED.';
    document.getElementById('aircraftOwnedSaveStatus').textContent = 'SAVED.';
  } catch (err) {
    alert('Could not save the profile. Check the connection (and that a database is configured) and retry');
  }
}

async function loadPirepLog() {
  const body = document.getElementById('pirepLogBody');
  try {
    const resp = await fetch('/pireps');
    pirepLogRecords = await resp.json();
    renderPirepLog();
  } catch (err) {
    body.innerHTML = '<div class="empty-state">COULD NOT LOAD PIREP LOG</div>';
  }
}

function renderPirepLog() {
  const body = document.getElementById('pirepLogBody');
  if (!pirepLogRecords.length) { body.innerHTML = '<div class="empty-state">No PIREPs filed yet</div>'; return; }
  body.innerHTML = pirepLogRecords.map(r => {
    const isOpen = r.id === expandedPirepId;
    const filedAt = r.pirep && r.pirep.submitted_at ? new Date(r.pirep.submitted_at) : new Date(r.submitted_at);
    const header = `
      <div class="leg-status-row" style="display:block;">
        <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px; flex-wrap:wrap;">
          <div style="cursor:pointer;" onclick="togglePirepRecord('${r.id}')">
            <strong>${escText(r.flight_number)} — ${escText(r.callsign || 'N/A')}</strong><br>
            <span class="placeholder-text">${r.departure_icao} &rarr; ${r.arrival_icao} &middot; FLIGHT DATE ${r.flight_date || 'N/A'} &middot; PIREP FILED ${filedAt.toUTCString()}</span>
          </div>
          <div>
            <button class="action small" onclick="togglePirepRecord('${r.id}')">${isOpen ? 'CLOSE' : 'OPEN'}</button>
            <button class="action small danger" onclick="deletePirepRecord('${r.id}')">DELETE</button>
          </div>
        </div>
        ${isOpen ? buildPirepDetailHtml(r) : ''}
      </div>`;
    return header;
  }).join('');
  if (expandedPirepId) {
    const rec = pirepLogRecords.find(r => r.id === expandedPirepId);
    if (rec && rec.leg && rec.leg.departure_info) initRouteMap(`map-pirep-${rec.id}`, rec.leg, rec.ofp);
  }
}

function buildPirepDetailHtml(r) {
  const leg = r.leg;
  if (!leg) return '<p class="placeholder-text" style="margin-top:8px;">NO FLIGHT DETAIL WAS SAVED WITH THIS PIREP</p>';
  const unit = (r.ofp && r.ofp.weight_unit) || 'KG';
  const mapId = `map-pirep-${r.id}`;
  return `
    ${routeMapDiv(leg, mapId)}
    ${legScheduleTable(leg)}
    ${legLoadTable(leg)}
    ${legWeatherBlock(leg)}
    ${r.ofp ? ofpSectionHtml(leg, { ofp: r.ofp }) : ''}
    ${legDelayMelBlock(leg)}
    ${r.loadsheet && r.loadsheet.text ? `<pre class="doc-sheet">${escText(r.loadsheet.text)}</pre>` : r.loadsheet ? `<div class="placeholder-text">LOADSHEET: ZFW ${fmtNum(r.loadsheet.zfw)} ${unit} &middot; FOB ${fmtNum(r.loadsheet.fob)} ${unit} &middot; ${fmtNum(r.loadsheet.pax)} PAX &middot; ${fmtNum(r.loadsheet.crew)} CREW</div>` : ''}
    ${r.pirep ? `<div class="placeholder-text">PIREP: AOBT ${r.pirep.aobt || 'N/A'}Z &middot; ATOT ${r.pirep.atot || 'N/A'}Z &middot; ALDT ${r.pirep.aldt}Z &middot; ABIT ${r.pirep.abit}Z &middot; AFAD ${fmtNum(r.pirep.afad)} ${unit}${pirepDelayCodesText(r.pirep)}</div>` : ''}
    ${r.pirep && r.pirep.remarks ? `<div class="placeholder-text">PILOT REMARKS: ${escText(r.pirep.remarks)}</div>` : ''}`;
}

function togglePirepRecord(id) {
  expandedPirepId = (expandedPirepId === id) ? null : id;
  renderPirepLog();
}

async function deletePirepRecord(id) {
  if (!confirm('Delete this PIREP record? This cannot be undone')) return;
  try {
    const resp = await fetch('/pireps/' + encodeURIComponent(id), { method: 'DELETE' });
    if (!resp.ok) throw new Error('delete failed');
    if (expandedPirepId === id) expandedPirepId = null;
    loadPirepLog();
  } catch (err) {
    alert('Could not delete this record. Check the connection and retry');
  }
}

function openGlossary() {
  document.getElementById('glossaryOverlay').classList.add('open');
  const filterInput = document.getElementById('glossaryFilter');
  filterInput.value = '';
  filterGlossary('');
  filterInput.focus();
}
function closeGlossary() {
  document.getElementById('glossaryOverlay').classList.remove('open');
}

function filterGlossary(query) {
  const q = query.trim().toLowerCase();
  let anyVisible = false;
  document.querySelectorAll('.glossary-cat').forEach(cat => {
    let catHasMatch = false;
    cat.querySelectorAll('.glossary-entry').forEach(entry => {
      const match = !q || entry.textContent.toLowerCase().includes(q);
      entry.classList.toggle('hidden', !match);
      if (match) catHasMatch = true;
    });
    cat.style.display = catHasMatch ? '' : 'none';
    if (catHasMatch) anyVisible = true;
  });
  document.getElementById('glossaryNoMatch').style.display = anyVisible ? 'none' : 'block';
}

function startNewFlight() {
  if (!confirm('Are you sure? This will discard the current flight')) return;
  clearActiveFlight();
  renderSearchUI();
}

// ---- Flight statistics page ----
// The whole logbook (GET /stats/flights, one compact row per PIREP - see
// flightstats.py) is loaded once; every control then filters and
// aggregates in the browser, so the page responds instantly.

const AIRCRAFT_TYPES = {
  '738': { name: 'Boeing 737-800', family: 'Boeing 737' },
  '319': { name: 'Airbus A319', family: 'Airbus A320 family' },
  '320': { name: 'Airbus A320', family: 'Airbus A320 family' },
  '32N': { name: 'Airbus A320neo', family: 'Airbus A320 family' },
  '321': { name: 'Airbus A321', family: 'Airbus A320 family' },
  '32Q': { name: 'Airbus A321neo', family: 'Airbus A320 family' },
};
const CARRIER_NAMES = { RYR: 'Ryanair', EZY: 'easyJet', WZZ: 'Wizz Air' };
const OPERATOR_NAMES = {
  RYR: 'Ryanair', RUK: 'Ryanair UK', EZY: 'easyJet UK', EJU: 'easyJet Europe', EZS: 'easyJet Switzerland',
  WZZ: 'Wizz Air', WMT: 'Wizz Air Malta', WUK: 'Wizz Air UK',
};
const STATS_PRESETS = [['7d', '7 DAYS'], ['30d', '30 DAYS'], ['90d', '90 DAYS'], ['ytd', 'YEAR TO DATE'], ['12m', '12 MONTHS'], ['all', 'ALL TIME']];
const STATS_TABS = [['overview', 'OVERVIEW'], ['fleet', 'FLEET & AIRLINES'], ['geo', 'GEOGRAPHY'], ['airports', 'AIRPORTS & ROUTES'], ['ops', 'OPERATIONS'], ['load', 'LOAD & FUEL'], ['records', 'RECORDS'], ['logbook', 'LOGBOOK']];
const STATS_METRICS = {
  flights: { label: 'FLIGHTS', value: rows => rows.length, fmt: v => fmtNum(v) },
  ft: { label: 'FLIGHT TIME', value: rows => sumOf(rows, 'ft'), fmt: v => fmtHours(v) },
  dist: { label: 'DISTANCE', value: rows => sumOf(rows, 'dist'), fmt: v => fmtNum(Math.round(v)) + ' nm' },
  pax: { label: 'PASSENGERS', value: rows => sumOf(rows, 'pax'), fmt: v => fmtNum(v) },
};
const STAT_BLUE = '#2a78d6', STAT_ORANGE = '#eb6834';
const STAT_FILTER_LABELS = { carrier: 'AIRLINE', type: 'AIRCRAFT', country: 'COUNTRY', airport: 'AIRPORT', scope: 'SCOPE' };

let statsData = null;
let statsState = null;
let statsMap = null;

function defaultStatsState() {
  return { preset: 'all', from: '', to: '', group: 'auto', metric: 'flights', tab: 'overview', timelineTable: false,
    f: { carrier: '', type: '', country: '', airport: '', scope: '' }, sort: { key: 'date', dir: 'desc' } };
}

// -- small helpers --
function sumOf(rows, key) { return rows.reduce((s, r) => s + (Number.isFinite(r[key]) ? r[key] : 0), 0); }
function fmtHours(min) {
  if (!Number.isFinite(min)) return 'N/A';
  const h = Math.floor(min / 60), m = Math.round(min % 60);
  return `${fmtNum(h)}h ${String(m).padStart(2, '0')}m`;
}
function fmtSigned(min) { return min == null ? '&mdash;' : (min > 0 ? '+' : '') + min + ' min'; }
function pct(n, d) { return d ? Math.round((n / d) * 100) : null; }
let regionNames = null;
function countryName(code) {
  if (!code) return 'Unknown';
  try { regionNames = regionNames || new Intl.DisplayNames(['en'], { type: 'region' }); return regionNames.of(code) || code; } catch (e) { return code; }
}
function typeName(t) { return (AIRCRAFT_TYPES[t] || {}).name || t || 'Unknown'; }
function typeFamily(t) { return (AIRCRAFT_TYPES[t] || {}).family || 'Other'; }
function statsAirport(icao) { return (statsData && statsData.airports[icao]) || {}; }
function rowCountries(r) { return [statsAirport(r.dep).country, statsAirport(r.arr).country]; }
function isDomestic(r) { const [a, b] = rowCountries(r); return !!a && a === b; }

// Dates are 'YYYY-MM-DD' strings, handled as UTC calendar days.
function isoDay(d) { return d.toISOString().slice(0, 10); }
function dayMs(s) { return Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)); }
function addDays(s, n) { return isoDay(new Date(dayMs(s) + n * 86400000)); }
function daysBetween(a, b) { return Math.round((dayMs(b) - dayMs(a)) / 86400000); }
function todayIso() { const d = new Date(); return isoDay(new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()))); }
function fmtDay(s, opts) { return new Date(dayMs(s)).toLocaleDateString('en-GB', Object.assign({ timeZone: 'UTC', day: 'numeric', month: 'short', year: 'numeric' }, opts || {})); }

function statsRange() {
  const s = statsState, today = todayIso();
  const first = statsData && statsData.flights.length ? statsData.flights[0].date : today;
  switch (s.preset) {
    case '7d': return [addDays(today, -6), today];
    case '30d': return [addDays(today, -29), today];
    case '90d': return [addDays(today, -89), today];
    case 'ytd': return [today.slice(0, 4) + '-01-01', today];
    case '12m': return [addDays(today, -364), today];
    case 'custom': return [s.from || first, s.to || today];
    default: return [first < today ? first : today, today];
  }
}
function previousRange([from, to]) {
  if (statsState.preset === 'all') return null;
  const len = daysBetween(from, to) + 1;
  return [addDays(from, -len), addDays(from, -1)];
}
function autoGroup([from, to]) {
  const d = daysBetween(from, to) + 1;
  return d <= 45 ? 'day' : d <= 200 ? 'week' : d <= 1100 ? 'month' : 'year';
}
function statsGroup(range) { return statsState.group === 'auto' ? autoGroup(range) : statsState.group; }

function rowMatches(r, range, f) {
  if (range && (!r.date || r.date < range[0] || r.date > range[1])) return false;
  if (f.carrier && r.carrier !== f.carrier) return false;
  if (f.type && r.type !== f.type) return false;
  if (f.country && !rowCountries(r).includes(f.country)) return false;
  if (f.airport && r.dep !== f.airport && r.arr !== f.airport) return false;
  if (f.scope === 'domestic' && !isDomestic(r)) return false;
  if (f.scope === 'international' && isDomestic(r)) return false;
  return true;
}

function statsKpis(rows) {
  const airports = new Set(), countries = new Set();
  rows.forEach(r => { [r.dep, r.arr].forEach(a => { if (a) { airports.add(a); const c = statsAirport(a).country; if (c) countries.add(c); } }); });
  const timed = rows.filter(r => r.dep_delay != null);
  return {
    flights: rows.length, ft: sumOf(rows, 'ft'), air: sumOf(rows, 'air'), dist: sumOf(rows, 'dist'), pax: sumOf(rows, 'pax'),
    airports: airports.size, countries: countries.size,
    otp: timed.length ? pct(timed.filter(r => r.dep_delay <= 15).length, timed.length) : null,
    est: rows.filter(r => r.ft_est).length,
  };
}

// Buckets covering the whole range (empty ones included, so gaps show).
function bucketStart(date, group) {
  if (group === 'day') return date;
  if (group === 'month') return date.slice(0, 7) + '-01';
  if (group === 'year') return date.slice(0, 4) + '-01-01';
  const dow = (new Date(dayMs(date)).getUTCDay() + 6) % 7;   // Monday = 0
  return addDays(date, -dow);
}
function bucketEnd(start, group) {
  if (group === 'day') return start;
  if (group === 'week') return addDays(start, 6);
  if (group === 'year') return start.slice(0, 4) + '-12-31';
  const d = new Date(dayMs(start)); return isoDay(new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)));
}
function bucketLabel(start, group, short) {
  if (group === 'year') return start.slice(0, 4);
  if (group === 'month') return fmtDay(start, short ? { day: undefined, year: '2-digit' } : { day: undefined });
  if (group === 'week') return short ? fmtDay(start, { year: undefined }) : `${fmtDay(start, { year: undefined })} – ${fmtDay(bucketEnd(start, 'week'))}`;
  return fmtDay(start, short ? { year: undefined } : { weekday: 'short' });
}
function statsBuckets(rows, range, group) {
  const buckets = [], byKey = {};
  for (let s = bucketStart(range[0], group); s <= range[1]; s = addDays(bucketEnd(s, group), 1)) {
    const b = { start: s, end: bucketEnd(s, group), rows: [] };
    buckets.push(b); byKey[s] = b;
    if (buckets.length > 400) break;
  }
  rows.forEach(r => { const b = byKey[bucketStart(r.date, group)]; if (b) b.rows.push(r); });
  return buckets;
}

// -- page --
async function renderStatsPage() {
  setAppNav('profile');
  setPageFit(true);
  if (!statsState) statsState = defaultStatsState();
  document.getElementById('mainContent').innerHTML = `<div class="panel"><div class="panel-body"><div class="empty-state">LOADING YOUR LOGBOOK&hellip;</div></div></div>`;
  try {
    statsData = await (await fetch('/stats/flights')).json();
  } catch (err) {
    document.getElementById('mainContent').innerHTML = `<div class="panel"><div class="empty-state">COULD NOT LOAD YOUR FLIGHTS. CHECK THE CONNECTION AND RETRY</div></div>`;
    return;
  }
  document.getElementById('mainContent').innerHTML = `
    <div class="panel st-controls">
      <div class="panel-header"><span>FILTERS AND PARAMETERS</span><button class="ghost small" type="button" onclick="renderUserPage()">&larr; PROFILE</button></div>
      <div class="panel-body">
        <div class="st-row">
          <div class="st-presets" role="group" aria-label="Date range">${STATS_PRESETS.map(([k, l]) => `<button type="button" class="st-chip" data-preset="${k}" onclick="setStatsPreset('${k}')">${l}</button>`).join('')}</div>
          <label class="st-field">FROM <input type="date" id="stFrom" onchange="setStatsDates()"></label>
          <label class="st-field">TO <input type="date" id="stTo" onchange="setStatsDates()"></label>
          <label class="st-field">GROUP BY <select id="stGroup" onchange="statsState.group = this.value; renderStatsBody()">
            <option value="auto">AUTO</option><option value="day">DAY</option><option value="week">WEEK</option><option value="month">MONTH</option><option value="year">YEAR</option></select></label>
        </div>
        <div class="st-row">
          ${['carrier', 'type', 'country', 'airport', 'scope'].map(k => `<label class="st-field">${STAT_FILTER_LABELS[k]} <select id="stF-${k}" onchange="setStatsFilter('${k}', this.value)"></select></label>`).join('')}
          <button class="ghost small" type="button" id="stReset" onclick="resetStatsFilters()">RESET FILTERS</button>
        </div>
      </div>
    </div>
    <div class="st-kpis" id="stKpis"></div>
    <div class="panel st-main">
      <div class="af-tabbar" role="tablist">${STATS_TABS.map(([k, l]) => `<button type="button" role="tab" id="st-tab-${k}" onclick="setStatsTab('${k}')">${l}</button>`).join('')}</div>
      <div class="st-content" id="stContent"></div>
    </div>
    <div class="st-tip" id="stTip" role="tooltip" hidden></div>`;
  renderStatsBody();
}

function setStatsPreset(p) { statsState.preset = p; renderStatsBody(); }
function setStatsDates() {
  const from = document.getElementById('stFrom').value, to = document.getElementById('stTo').value;
  if (!from || !to) return;
  statsState.preset = 'custom';
  statsState.from = from <= to ? from : to;
  statsState.to = from <= to ? to : from;
  renderStatsBody();
}
function setStatsFilter(key, value) { statsState.f[key] = value; renderStatsBody(); }
function resetStatsFilters() { statsState.f = defaultStatsState().f; renderStatsBody(); }
function setStatsTab(tab) { statsState.tab = tab; renderStatsBody(); }
// Zoom into one bar of the timeline: its period becomes the range and the
// grouping steps down (year -> months -> weeks/days).
function drillStats(start, end) {
  statsState.preset = 'custom';
  statsState.from = start;
  statsState.to = end > todayIso() ? todayIso() : end;
  statsState.group = 'auto';
  renderStatsBody();
}

function statsFilterOptions(key, rows) {
  const counts = new Map();
  const add = (v) => { if (v) counts.set(v, (counts.get(v) || 0) + 1); };
  rows.forEach(r => {
    if (key === 'carrier') add(r.carrier);
    else if (key === 'type') add(r.type);
    else if (key === 'country') new Set(rowCountries(r)).forEach(add);
    else if (key === 'airport') { add(r.dep); if (r.arr !== r.dep) add(r.arr); }
  });
  const label = (v) => key === 'carrier' ? (CARRIER_NAMES[v] || v) : key === 'type' ? typeName(v)
    : key === 'country' ? countryName(v) : `${v} ${statsAirport(v).city || statsAirport(v).name || ''}`.trim();
  return [...counts.entries()].map(([v, n]) => [v, `${label(v)} (${n})`]).sort((a, b) => a[1].localeCompare(b[1]));
}

function renderStatsBody() {
  if (!document.getElementById('stKpis')) return;
  const s = statsState, all = statsData.flights;
  const range = statsRange(), group = statsGroup(range), prev = previousRange(range);
  const rows = all.filter(r => rowMatches(r, range, s.f));
  const prevRows = prev ? all.filter(r => rowMatches(r, prev, s.f)) : null;

  // controls
  document.querySelectorAll('.st-chip').forEach(b => { const on = b.dataset.preset === s.preset; b.classList.toggle('on', on); b.setAttribute('aria-pressed', on); });
  document.getElementById('stFrom').value = range[0];
  document.getElementById('stTo').value = range[1];
  document.getElementById('stGroup').value = s.group;
  document.querySelector('#stGroup option[value="auto"]').textContent = `AUTO (${group.toUpperCase()})`;
  // Each filter lists what exists in the range under the other filters.
  ['carrier', 'type', 'country', 'airport'].forEach(k => {
    const others = Object.assign({}, s.f, { [k]: '' });
    const opts = statsFilterOptions(k, all.filter(r => rowMatches(r, range, others)));
    if (s.f[k] && !opts.some(([v]) => v === s.f[k])) opts.unshift([s.f[k], k === 'type' ? typeName(s.f[k]) : k === 'country' ? countryName(s.f[k]) : s.f[k]]);
    const sel = document.getElementById('stF-' + k);
    sel.innerHTML = `<option value="">ALL</option>` + opts.map(([v, l]) => `<option value="${escAttr(v)}">${escText(l)}</option>`).join('');
    sel.value = s.f[k];
    sel.classList.toggle('is-set', !!s.f[k]);
  });
  const scope = document.getElementById('stF-scope');
  scope.innerHTML = `<option value="">ALL</option><option value="domestic">DOMESTIC</option><option value="international">INTERNATIONAL</option>`;
  scope.value = s.f.scope;
  scope.classList.toggle('is-set', !!s.f.scope);
  document.getElementById('stReset').disabled = !Object.values(s.f).some(Boolean);
  STATS_TABS.forEach(([k]) => { const b = document.getElementById('st-tab-' + k); b.classList.toggle('on', k === s.tab); b.setAttribute('aria-selected', k === s.tab); });

  renderStatsKpis(rows, prevRows, range);
  const content = document.getElementById('stContent');
  if (statsMap) { statsMap.remove(); statsMap = null; }
  if (!all.length) {
    content.innerHTML = `<div class="empty-state">No flights logged yet. File a PIREP at the end of a flight and it appears here</div>`;
    return;
  }
  if (!rows.length) {
    content.innerHTML = `<div class="empty-state">No flights match this date range and these filters</div>`;
    return;
  }
  const views = { overview: statsOverview, fleet: statsFleet, geo: statsGeo, airports: statsAirports, ops: statsOperations, load: statsLoadFuel, records: statsRecords, logbook: statsLogbook };
  views[s.tab](content, rows, range, group);
}

function renderStatsKpis(rows, prevRows, range) {
  const k = statsKpis(rows), p = prevRows ? statsKpis(prevRows) : null;
  // One grey line per tile: the change vs the previous period when there
  // is one (the full period name is in its tooltip), else the tile's note.
  const periodName = { '7d': 'the previous 7 days', '30d': 'the previous 30 days', '90d': 'the previous 90 days', ytd: 'the same number of days before', '12m': 'the previous 12 months', custom: 'the previous period of the same length' }[statsState.preset];
  const delta = (key, points) => {
    if (!p || k[key] == null || p[key] == null) return '';
    const arrow = d => d > 0 ? '&#9650; +' : d < 0 ? '&#9660; ' : '';
    if (points) { const d = k[key] - p[key]; return `${arrow(d)}${d} pts vs previous`; }
    if (!p[key]) return k[key] ? 'new vs previous' : '';
    const d = Math.round(((k[key] - p[key]) / p[key]) * 100);
    return `${arrow(d)}${d}% vs previous`;
  };
  const tile = (label, value, key, note = '', points = false, noteTitle = '') => {
    const d = delta(key, points);
    const line = d || note;
    const title = d ? `Compared with ${periodName}` : noteTitle;
    return `<div class="st-kpi"><span class="st-kpi-label">${label}</span><span class="st-kpi-value">${value}</span>${line ? `<span class="st-delta"${title ? ` title="${escAttr(title)}"` : ''}>${line}</span>` : ''}</div>`;
  };
  const estTitle = 'PIREPs without actual block times count their scheduled block time';
  document.getElementById('stKpis').innerHTML = [
    tile('Flights', fmtNum(k.flights), 'flights'),
    tile('Flight time', fmtHours(k.ft) + (k.est && p ? `<sup class="st-est" title="${k.est} estimated: ${estTitle}">*</sup>` : ''), 'ft', k.est ? `${k.est} estimated` : '', false, estTitle),
    tile('Air time', fmtHours(k.air), 'air'),
    tile('Distance', fmtNum(Math.round(k.dist)) + ' nm', 'dist'),
    tile('Passengers', fmtNum(k.pax), 'pax'),
    tile('Airports', fmtNum(k.airports), 'airports'),
    tile('Countries', fmtNum(k.countries), 'countries'),
    tile('On-time departures', k.otp == null ? '&mdash;' : k.otp + '%', 'otp', 'within 15 min', true, 'Off-block within 15 minutes of schedule'),
  ].join('');
}

// Shared hover tooltip: values lead, labels follow.
function statsTip(e, html) {
  const tip = document.getElementById('stTip');
  if (!tip) return;
  if (!html) { tip.hidden = true; return; }
  tip.innerHTML = html;
  tip.hidden = false;
  const x = (e.clientX != null ? e.clientX : e.target.getBoundingClientRect().left), y = (e.clientY != null ? e.clientY : e.target.getBoundingClientRect().top);
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = Math.min(window.innerWidth - w - 8, Math.max(8, x + 14)) + 'px';
  tip.style.top = Math.max(8, y - h - 12) + 'px';
}

// Ranked horizontal bars (HTML): label, value, share bar; a row click
// applies it as a filter.
function statsBars(items, { value, fmt, onPick, pickLabel }) {
  const max = Math.max(...items.map(value), 1);
  return `<div class="st-bars">${items.map((it, i) => `
    <button type="button" class="st-bar-row"${onPick ? ` onclick="${onPick(it)}" title="${escAttr(pickLabel || 'Filter by this')}"` : ' disabled'}>
      <span class="st-bar-label">${it.label}</span>
      <span class="st-bar-track"><span class="st-bar-fill" style="width:${Math.max(1, (value(it) / max) * 100)}%"></span></span>
      <span class="st-bar-value">${fmt(value(it))}</span>
    </button>`).join('')}</div>`;
}

function groupRows(rows, keyFn) {
  const m = new Map();
  rows.forEach(r => { (Array.isArray(keyFn(r)) ? keyFn(r) : [keyFn(r)]).forEach(k => { if (!k) return; if (!m.has(k)) m.set(k, []); m.get(k).push(r); }); });
  return m;
}

// -- Overview: timeline + at a glance --
function statsOverview(el, rows, range, group) {
  const s = statsState;
  const byType = [...groupRows(rows, r => r.type)].map(([t, rs]) => ({ label: escText(typeName(t)), key: t, rows: rs })).sort((a, b) => b.rows.length - a.rows.length).slice(0, 5);
  const movements = new Map();
  rows.forEach(r => [r.dep, r.arr].forEach(a => movements.set(a, (movements.get(a) || 0) + 1)));
  const topAirports = [...movements].sort((a, b) => b[1] - a[1]).slice(0, 5).map(([a, n]) => ({ label: `<b>${a}</b> ${escText(statsAirport(a).city || statsAirport(a).name || '')}`, key: a, n }));
  const dom = rows.filter(isDomestic).length, intl = rows.length - dom;
  el.innerHTML = `
    <div class="st-grid st-grid-overview">
      <section class="st-card st-card-grow">
        <div class="st-card-head">
          <h3>${s.timelineTable ? 'ALL MEASURES' : STATS_METRICS[s.metric].label} BY ${group.toUpperCase()}</h3>
          <div class="st-seg" role="group" aria-label="Measure">${Object.entries(STATS_METRICS).map(([k, m]) => `<button type="button" class="st-chip${k === s.metric && !s.timelineTable ? ' on' : ''}" aria-pressed="${k === s.metric && !s.timelineTable}"${s.timelineTable ? ' disabled title="The table shows every measure"' : ''} onclick="statsState.metric='${k}'; renderStatsBody()">${m.label}</button>`).join('')}
            <label class="switch-row st-switch"><span class="switch-toggle"><input type="checkbox" id="stTableSwitch"${s.timelineTable ? ' checked' : ''} onchange="statsState.timelineTable = this.checked; renderStatsBody()"><span class="track"></span></span>TABLE</label></div>
        </div>
        <p class="st-hint">${s.timelineTable ? 'Every measure per period, with periods that have flights' : group === 'day' ? 'Hover a bar for its flights' : 'Click a bar to zoom into that period'}</p>
        <div class="st-timeline" id="stTimeline"></div>
      </section>
      <section class="st-card">
        <h3>AIRCRAFT</h3>
        ${statsBars(byType, { value: it => it.rows.length, fmt: v => fmtNum(v) + (v === 1 ? ' flight' : ' flights'), onPick: it => `setStatsFilter('type', '${escAttr(it.key)}')` })}
        <h3>AIRPORTS</h3>
        ${statsBars(topAirports, { value: it => it.n, fmt: v => fmtNum(v) + ' mvts', onPick: it => `setStatsFilter('airport', '${it.key}')` })}
        <h3>DOMESTIC / INTERNATIONAL</h3>
        ${statsSplit([{ label: 'Domestic', n: dom, color: STAT_BLUE, pick: 'domestic' }, { label: 'International', n: intl, color: STAT_ORANGE, pick: 'international' }])}
      </section>
    </div>`;
  const buckets = statsBuckets(rows, range, group);
  if (s.timelineTable) statsTimelineTable(document.getElementById('stTimeline'), buckets, group);
  else statsTimelineChart(document.getElementById('stTimeline'), buckets, group);
}

function statsSplit(parts) {
  const total = parts.reduce((s, p) => s + p.n, 0) || 1;
  return `<div class="st-split" role="img" aria-label="${parts.map(p => `${p.label} ${p.n}`).join(', ')}">${parts.filter(p => p.n).map(p =>
    `<button type="button" class="st-split-seg" style="flex:${p.n}; background:${p.color}" onclick="setStatsFilter('scope', '${p.pick}')" title="${p.label}: ${p.n} (${Math.round(p.n / total * 100)}%)"></button>`).join('')}</div>
    <div class="st-legend">${parts.map(p => `<span><i style="background:${p.color}"></i>${p.label} <b>${fmtNum(p.n)}</b> (${Math.round(p.n / total * 100)}%)</span>`).join('')}</div>`;
}

function niceTicks(max) {
  if (max <= 0) return [0, 1];
  const raw = max / 4, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(st => st >= raw);
  const ticks = [];
  for (let v = 0; v <= max + step * 0.001; v += step) ticks.push(v);
  if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step);
  return ticks;
}

function statsTimelineChart(host, buckets, group) {
  const m = STATS_METRICS[statsState.metric];
  const values = buckets.map(b => m.value(b.rows));
  const w = Math.max(320, host.clientWidth), h = Math.max(180, host.clientHeight || 260);
  const pad = { l: 56, r: 12, t: 12, b: 28 };
  const ticks = niceTicks(Math.max(...values));
  const top = ticks[ticks.length - 1] || 1;
  const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
  const slot = pw / buckets.length, bw = Math.max(2, Math.min(24, slot - 2));
  const y = v => pad.t + ph - (v / top) * ph;
  const tickFmt = statsState.metric === 'ft' ? (v => Math.round(v / 60) + 'h') : (v => v >= 10000 ? Math.round(v / 1000) + 'k' : fmtNum(Math.round(v)));
  const every = Math.max(1, Math.ceil(buckets.length / Math.floor(pw / 64)));
  let svg = `<svg width="${w}" height="${h}" role="img" aria-label="${m.label} by ${group}">`;
  ticks.forEach(t => { svg += `<line class="st-grid-line" x1="${pad.l}" x2="${w - pad.r}" y1="${y(t)}" y2="${y(t)}"/><text class="st-axis" x="${pad.l - 8}" y="${y(t) + 4}" text-anchor="end">${tickFmt(t)}</text>`; });
  buckets.forEach((b, i) => {
    const x = pad.l + i * slot + (slot - bw) / 2, v = values[i], bh = (v / top) * ph;
    const r = Math.min(4, bw / 2, bh);
    if (v > 0) svg += `<path class="st-col" d="M${x},${pad.t + ph} V${y(v) + r} Q${x},${y(v)} ${x + r},${y(v)} H${x + bw - r} Q${x + bw},${y(v)} ${x + bw},${y(v) + r} V${pad.t + ph} Z" fill="${STAT_BLUE}"/>`;
    svg += `<rect class="st-hit" data-i="${i}" x="${pad.l + i * slot}" y="${pad.t}" width="${slot}" height="${ph}" tabindex="0" aria-label="${escAttr(bucketLabel(b.start, group))}: ${escAttr(m.fmt(v))}"/>`;
    if (i % every === 0) svg += `<text class="st-axis" x="${pad.l + i * slot + slot / 2}" y="${h - 8}" text-anchor="middle">${escText(bucketLabel(b.start, group, true))}</text>`;
  });
  svg += `<line class="st-baseline" x1="${pad.l}" x2="${w - pad.r}" y1="${pad.t + ph}" y2="${pad.t + ph}"/></svg>`;
  host.innerHTML = svg;
  host.querySelectorAll('.st-hit').forEach(hit => {
    const b = buckets[+hit.dataset.i], v = values[+hit.dataset.i];
    const html = () => `<b>${m.fmt(v)}</b><span>${escText(bucketLabel(b.start, group))}</span>` + (group === 'day' && b.rows.length
      ? `<ul>${b.rows.slice(0, 6).map(r => `<li>${escText(r.fn)} ${r.dep}&rarr;${r.arr}</li>`).join('')}</ul>` : '') + (group !== 'day' && b.rows.length ? '<em>Click to zoom in</em>' : '');
    hit.addEventListener('pointermove', e => { hit.classList.add('on'); statsTip(e, html()); });
    hit.addEventListener('pointerleave', () => { hit.classList.remove('on'); statsTip(null, ''); });
    hit.addEventListener('focus', e => statsTip({ target: hit }, html()));
    hit.addEventListener('blur', () => statsTip(null, ''));
    if (group !== 'day') {
      const go = () => { statsTip(null, ''); drillStats(b.start, b.end); };
      hit.addEventListener('click', go);
      hit.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
    }
  });
}

function statsTimelineTable(host, buckets, group) {
  host.innerHTML = `<div class="st-scroll"><table class="st-table"><thead><tr><th>${group.toUpperCase()}</th>${Object.values(STATS_METRICS).map(m => `<th class="num">${m.label}</th>`).join('')}</tr></thead><tbody>
    ${buckets.filter(b => b.rows.length).map(b => `<tr><td>${escText(bucketLabel(b.start, group))}</td>${Object.values(STATS_METRICS).map(m => `<td class="num">${m.fmt(m.value(b.rows))}</td>`).join('')}</tr>`).join('')}
  </tbody></table></div>`;
}

// -- Fleet & airlines --
function statsFleet(el, rows) {
  const total = rows.length, ftTotal = sumOf(rows, 'ft') || 1;
  const typeRows = [...groupRows(rows, r => r.type)].map(([t, rs]) => ({ t, rs, family: typeFamily(t) }));
  const families = [...groupRows(typeRows, x => x.family)].sort((a, b) => sumOf(b[1].flatMap(x => x.rs), 'ft') - sumOf(a[1].flatMap(x => x.rs), 'ft'));
  const avg = (rs, k) => { const v = rs.filter(r => Number.isFinite(r[k])); return v.length ? sumOf(v, k) / v.length : null; };
  const line = (label, rs, pick, cls) => `<tr class="${cls || ''}"${pick ? ` onclick="${pick}" tabindex="0" title="Filter by this"` : ''}>
      <td>${label}</td><td class="num">${fmtNum(rs.length)}</td><td class="num">${fmtHours(sumOf(rs, 'ft'))}</td>
      <td class="st-share"><span class="st-bar-track"><span class="st-bar-fill" style="width:${(sumOf(rs, 'ft') / ftTotal) * 100}%"></span></span>${Math.round((sumOf(rs, 'ft') / ftTotal) * 100)}%</td>
      <td class="num">${avg(rs, 'dist') != null ? fmtNum(Math.round(avg(rs, 'dist'))) + ' nm' : '&mdash;'}</td><td class="num">${avg(rs, 'ft') != null ? fmtHours(avg(rs, 'ft')) : '&mdash;'}</td></tr>`;
  const head = (first) => `<thead><tr><th>${first}</th><th class="num">FLIGHTS</th><th class="num">FLIGHT TIME</th><th>SHARE OF TIME</th><th class="num">AVG SECTOR</th><th class="num">AVG FLIGHT</th></tr></thead>`;
  const carriers = [...groupRows(rows, r => r.carrier)].sort((a, b) => b[1].length - a[1].length);
  el.innerHTML = `
    <div class="st-grid st-grid-2">
      <section class="st-card st-card-scroll"><h3>AIRCRAFT</h3><div class="st-scroll"><table class="st-table st-clickable">${head('TYPE')}<tbody>
        ${families.map(([fam, types]) => line(`<b>${escText(fam)}</b>`, types.flatMap(x => x.rs), '', 'st-group') +
          types.sort((a, b) => b.rs.length - a.rs.length).map(x => line(`<span class="st-indent">${escText(typeName(x.t))}</span>`, x.rs, `setStatsFilter('type', '${escAttr(x.t)}')`)).join('')).join('')}
      </tbody></table></div></section>
      <section class="st-card st-card-scroll"><h3>AIRLINES &amp; OPERATORS</h3><div class="st-scroll"><table class="st-table st-clickable">${head('AIRLINE')}<tbody>
        ${carriers.map(([c, rs]) => line(`<b>${escText(CARRIER_NAMES[c] || c || 'Unknown')}</b>`, rs, c ? `setStatsFilter('carrier', '${escAttr(c)}')` : '', 'st-group') +
          [...groupRows(rs, r => r.op || '')].filter(([op]) => op).sort((a, b) => b[1].length - a[1].length)
            .map(([op, ors]) => line(`<span class="st-indent">${escText(OPERATOR_NAMES[op] || op)} <span class="st-muted">${escText(op)}</span></span>`, ors, '')).join('')).join('')}
      </tbody></table></div>
      <p class="st-hint st-hint-right">${total} flights. Operators come from the callsign flown (e.g. WMT for Wizz Air Malta)</p></section>
    </div>`;
}

// -- Geography --
function statsGeo(el, rows) {
  const countries = new Map();
  rows.forEach(r => new Set(rowCountries(r).filter(Boolean)).forEach(c => {
    if (!countries.has(c)) countries.set(c, { flights: 0, airports: new Set() });
    const e = countries.get(c); e.flights++;
    [r.dep, r.arr].forEach(a => { if (statsAirport(a).country === c) e.airports.add(a); });
  }));
  const firstVisit = {};
  statsData.flights.forEach(r => rowCountries(r).forEach(c => { if (c && !firstVisit[c]) firstVisit[c] = r.date; }));
  const dom = rows.filter(isDomestic).length;
  el.innerHTML = `
    <div class="st-grid st-grid-geo">
      <section class="st-card st-card-grow"><h3>ROUTES FLOWN</h3><p class="st-hint">Line weight grows with flights on the route; circle size with airport movements. Click an airport to filter</p><div class="st-map" id="stMap"></div></section>
      <section class="st-card st-card-scroll">
        <h3>DOMESTIC / INTERNATIONAL</h3>
        ${statsSplit([{ label: 'Domestic', n: dom, color: STAT_BLUE, pick: 'domestic' }, { label: 'International', n: rows.length - dom, color: STAT_ORANGE, pick: 'international' }])}
        <h3>COUNTRIES</h3>
        <div class="st-scroll"><table class="st-table st-clickable"><thead><tr><th>COUNTRY</th><th class="num">FLIGHTS</th><th class="num">AIRPORTS</th><th class="num">FIRST VISIT</th></tr></thead><tbody>
          ${[...countries].sort((a, b) => b[1].flights - a[1].flights).map(([c, e]) => `<tr onclick="setStatsFilter('country', '${escAttr(c)}')" tabindex="0" title="Filter by this"><td>${escText(countryName(c))} <span class="st-muted">${escText(c)}</span></td><td class="num">${fmtNum(e.flights)}</td><td class="num">${e.airports.size}</td><td class="num">${firstVisit[c] ? fmtDay(firstVisit[c]) : ''}</td></tr>`).join('')}
        </tbody></table></div>
      </section>
    </div>`;
  if (typeof L === 'undefined') return;
  statsMap = L.map('stMap', { zoomControl: true, maxZoom: BASEMAP_MAX_ZOOM, scrollWheelZoom: window.matchMedia('(hover: hover) and (pointer: fine)').matches, wheelPxPerZoomLevel: 120 });
  addBasemap(statsMap);
  const routes = groupRows(rows, r => [r.dep, r.arr].sort().join('-'));
  const moves = new Map();
  rows.forEach(r => [r.dep, r.arr].forEach(a => moves.set(a, (moves.get(a) || 0) + 1)));
  const pts = [];
  routes.forEach((rs, key) => {
    const [a, b] = key.split('-').map(statsAirport);
    if (a.lat == null || b.lat == null) return;
    L.polyline([[a.lat, a.lon], [b.lat, b.lon]], { color: STAT_BLUE, weight: 1.5 + Math.min(4, Math.log2(rs.length)), opacity: 0.55 })
      .bindTooltip(`<b>${rs.length}</b> ${rs.length === 1 ? 'flight' : 'flights'} &middot; ${key.replace('-', ' &ndash; ')}`, { sticky: true }).addTo(statsMap);
  });
  const maxMoves = Math.max(...moves.values());
  moves.forEach((n, icao) => {
    const a = statsAirport(icao);
    if (a.lat == null) return;
    pts.push([a.lat, a.lon]);
    L.circleMarker([a.lat, a.lon], { radius: 4 + 8 * Math.sqrt(n / maxMoves), color: '#FFFFFF', weight: 2, fillColor: STAT_BLUE, fillOpacity: 1 })
      .bindTooltip(`<b>${n}</b> movements &middot; ${icao} ${escText(a.name || '')}`).on('click', () => setStatsFilter('airport', icao)).addTo(statsMap);
  });
  if (pts.length === 1) statsMap.setView(pts[0], 6);
  else if (pts.length) statsMap.fitBounds(L.latLngBounds(pts), { padding: [24, 24], maxZoom: 7 });
  else statsMap.setView([48, 10], 4);
  if (window.ResizeObserver) { const m = statsMap; new ResizeObserver(() => { if (m === statsMap) m.invalidateSize(); }).observe(document.getElementById('stMap')); }
}

// -- Airports & routes --
function statsAirports(el, rows, range) {
  const firstSeen = {};
  statsData.flights.forEach(r => [r.dep, r.arr].forEach(a => { if (a && !firstSeen[a]) firstSeen[a] = r.date; }));
  const ap = new Map();
  rows.forEach(r => { [['dep', r.dep], ['arr', r.arr]].forEach(([k, a]) => { if (!ap.has(a)) ap.set(a, { dep: 0, arr: 0 }); ap.get(a)[k]++; }); });
  const newOnes = [...ap.keys()].filter(a => firstSeen[a] >= range[0] && firstSeen[a] <= range[1]).sort((a, b) => firstSeen[a].localeCompare(firstSeen[b]));
  const routes = [...groupRows(rows, r => [r.dep, r.arr].sort().join('-'))].map(([k, rs]) => ({ k, rs, dist: rs.find(r => r.dist != null)?.dist ?? null }));
  const byDist = rows.filter(r => r.dist != null).sort((a, b) => b.dist - a.dist);
  const apLabel = a => `<b>${a}</b> ${escText(statsAirport(a).city || statsAirport(a).name || '')}`;
  const flightLine = r => `<tr><td>${escText(r.fn)}</td><td>${r.dep} &rarr; ${r.arr}</td><td class="num">${fmtNum(r.dist)} nm</td><td class="num">${fmtDay(r.date)}</td></tr>`;
  el.innerHTML = `
    <div class="st-grid st-grid-3">
      <section class="st-card st-card-scroll"><h3>AIRPORTS <span class="st-muted">${ap.size}</span></h3><div class="st-scroll"><table class="st-table st-clickable"><thead><tr><th>AIRPORT</th><th class="num">DEP</th><th class="num">ARR</th><th class="num">FIRST VISIT</th></tr></thead><tbody>
        ${[...ap].sort((a, b) => (b[1].dep + b[1].arr) - (a[1].dep + a[1].arr)).map(([a, c]) => `<tr onclick="setStatsFilter('airport', '${a}')" tabindex="0" title="Filter by this"><td>${apLabel(a)}<span class="st-sub">${escText(countryName(statsAirport(a).country))}</span></td><td class="num">${c.dep}</td><td class="num">${c.arr}</td><td class="num">${fmtDay(firstSeen[a])}</td></tr>`).join('')}
      </tbody></table></div></section>
      <section class="st-card st-card-scroll"><h3>ROUTES <span class="st-muted">${routes.length}</span></h3><div class="st-scroll"><table class="st-table"><thead><tr><th>CITY PAIR</th><th class="num">FLIGHTS</th><th class="num">DISTANCE</th></tr></thead><tbody>
        ${routes.sort((a, b) => b.rs.length - a.rs.length || (b.dist || 0) - (a.dist || 0)).map(x => { const [a, b] = x.k.split('-'); return `<tr><td>${apLabel(a)} &ndash; ${apLabel(b)}</td><td class="num">${x.rs.length}</td><td class="num">${x.dist != null ? fmtNum(x.dist) + ' nm' : '&mdash;'}</td></tr>`; }).join('')}
      </tbody></table></div></section>
      <section class="st-card st-card-stack">
        <h3>NEW AIRPORTS IN THIS PERIOD <span class="st-muted">${newOnes.length}</span></h3>
        ${newOnes.length ? `<ul class="st-list">${newOnes.map(a => `<li>${apLabel(a)} <span class="st-muted">${fmtDay(firstSeen[a])}</span></li>`).join('')}</ul>` : '<p class="st-hint">No first visits in this period</p>'}
        <h3>LONGEST FLIGHTS</h3><div class="st-scroll st-scroll-short"><table class="st-table"><tbody>${byDist.slice(0, 5).map(flightLine).join('')}</tbody></table></div>
        <h3>SHORTEST FLIGHTS</h3><div class="st-scroll st-scroll-short"><table class="st-table"><tbody>${byDist.slice(-5).reverse().map(flightLine).join('')}</tbody></table></div>
      </section>
    </div>`;
}

// -- Operations: punctuality, delays, MEL, when you fly --
const DELAY_BANDS = [['Early', -Infinity, 0], ['1–5 min', 1, 5], ['6–15 min', 6, 15], ['16–30 min', 16, 30], ['31–60 min', 31, 60], ['Over 60 min', 61, Infinity]];
const DOW_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

function statsMiniCols(items, fmt) {
  // Small single-series column chart in HTML: label under each column,
  // value in the hover title and in the visible label above the tallest.
  const max = Math.max(...items.map(i => i.v), 1);
  const peak = items.reduce((b, i) => (i.v > b.v ? i : b), items[0]);
  return `<div class="st-cols" role="img" aria-label="${escAttr(items.map(i => `${i.label}: ${fmt(i.v)}`).join(', '))}">${items.map(i => `
    <div class="st-colwrap" title="${escAttr(i.label)}: ${escAttr(fmt(i.v))}">
      <span class="st-colval">${i === peak && i.v ? fmt(i.v) : ''}</span>
      <span class="st-colbar" style="height:${i.v ? Math.max(2, (i.v / max) * 100) : 0}%"></span>
      <span class="st-collabel">${i.short != null ? i.short : i.label}</span>
    </div>`).join('')}</div>`;
}

function statsOperations(el, rows) {
  const dep = rows.filter(r => r.dep_delay != null), arr = rows.filter(r => r.arr_delay != null);
  const avg = (xs, k) => xs.length ? Math.round(sumOf(xs, k) / xs.length) : null;
  const otp = (xs, k) => xs.length ? pct(xs.filter(r => r[k] <= 15).length, xs.length) : null;
  const bands = DELAY_BANDS.map(([label, lo, hi]) => ({ label, short: label.replace(' min', ''), v: dep.filter(r => r.dep_delay >= lo && r.dep_delay <= hi).length }));
  const codes = [...groupRows(rows.filter(r => r.delay_code), r => r.delay_code)].sort((a, b) => b[1].length - a[1].length);
  const mels = [...groupRows(rows.filter(r => r.mel), r => r.mel)].sort((a, b) => b[1].length - a[1].length);
  const hours = Array.from({ length: 24 }, (_, h) => ({ label: `${String(h).padStart(2, '0')}:00Z`, short: h % 3 === 0 ? String(h).padStart(2, '0') : '', v: rows.filter(r => r.sobt != null && Math.floor(r.sobt / 60) === h).length }));
  const dows = DOW_NAMES.map((d, i) => ({ label: d, short: d.slice(0, 3), v: rows.filter(r => r.date && (new Date(dayMs(r.date)).getUTCDay() + 6) % 7 === i).length }));
  const byAirport = [...groupRows(dep, r => r.dep)].filter(([, rs]) => rs.length >= 2)
    .map(([a, rs]) => ({ a, n: rs.length, otp: otp(rs, 'dep_delay'), avg: avg(rs, 'dep_delay') })).sort((x, y) => y.avg - x.avg);
  const stat = (label, value, note) => `<div class="st-mini"><span>${label}</span><b>${value}</b>${note ? `<em>${note}</em>` : ''}</div>`;
  el.innerHTML = `
    <div class="st-grid st-grid-3">
      <section class="st-card st-card-stack">
        <h3>PUNCTUALITY</h3>
        <div class="st-minis">
          ${stat('On-time departures', otp(dep, 'dep_delay') == null ? '&mdash;' : otp(dep, 'dep_delay') + '%', 'off-block within 15 min')}
          ${stat('On-time arrivals', otp(arr, 'arr_delay') == null ? '&mdash;' : otp(arr, 'arr_delay') + '%', 'in-block within 15 min')}
          ${stat('Average departure delay', avg(dep, 'dep_delay') == null ? '&mdash;' : fmtSigned(avg(dep, 'dep_delay')), `${dep.length} flights with times`)}
          ${stat('Average arrival delay', avg(arr, 'arr_delay') == null ? '&mdash;' : fmtSigned(avg(arr, 'arr_delay')), 'vs scheduled in-block')}
        </div>
        <h3>DEPARTURE DELAY VS SCHEDULE</h3>
        ${dep.length ? statsMiniCols(bands, v => `${v} ${v === 1 ? 'flight' : 'flights'}`) : '<p class="st-hint">No flights with actual off-block times in this selection</p>'}
      </section>
      <section class="st-card st-card-stack">
        <h3>DISPATCHED DELAYS <span class="st-muted">${rows.filter(r => r.delay_code).length} of ${rows.length} flights</span></h3>
        ${codes.length ? `<div class="st-scroll st-scroll-short"><table class="st-table"><thead><tr><th>CODE</th><th>REASON</th><th class="num">FLIGHTS</th></tr></thead><tbody>
          ${codes.map(([c, rs]) => `<tr><td><b>${escText(c)}</b></td><td>${escText(rs.find(r => r.delay_desc)?.delay_desc || '')}</td><td class="num">${rs.length}</td></tr>`).join('')}</tbody></table></div>` : '<p class="st-hint">No delays were dispatched on these flights</p>'}
        <h3>MEL ITEMS <span class="st-muted">${rows.filter(r => r.mel).length} of ${rows.length} flights</span></h3>
        ${mels.length ? statsBars(mels.map(([m, rs]) => ({ label: escText(m), n: rs.length })), { value: it => it.n, fmt: v => `${v} ${v === 1 ? 'flight' : 'flights'}` }) : '<p class="st-hint">No MEL items on these flights</p>'}
        <h3>DEPARTURE PUNCTUALITY BY AIRPORT <span class="st-muted">2+ departures, worst first</span></h3>
        ${byAirport.length ? `<div class="st-scroll st-scroll-short"><table class="st-table st-clickable"><thead><tr><th>AIRPORT</th><th class="num">DEPARTURES</th><th class="num">ON TIME</th><th class="num">AVG DELAY</th></tr></thead><tbody>
          ${byAirport.map(x => `<tr onclick="setStatsFilter('airport', '${x.a}')" tabindex="0" title="Filter by this"><td><b>${x.a}</b> ${escText(statsAirport(x.a).city || '')}</td><td class="num">${x.n}</td><td class="num">${x.otp}%</td><td class="num">${fmtSigned(x.avg)}</td></tr>`).join('')}</tbody></table></div>` : '<p class="st-hint">Not enough departures per airport yet</p>'}
      </section>
      <section class="st-card st-card-stack">
        <h3>SCHEDULED DEPARTURE TIME (UTC)</h3>
        ${statsMiniCols(hours, v => `${v} ${v === 1 ? 'flight' : 'flights'}`)}
        <h3>DAY OF THE WEEK</h3>
        ${statsMiniCols(dows, v => `${v} ${v === 1 ? 'flight' : 'flights'}`)}
      </section>
    </div>`;
}

// -- Load & fuel --
function statsLoadFuel(el, rows) {
  const withLf = rows.filter(r => Number.isFinite(r.lf));
  const avgLf = withLf.length ? Math.round((sumOf(withLf, 'lf') / withLf.length) * 100) : null;
  const byType = [...groupRows(rows, r => r.type)].map(([t, rs]) => {
    const lf = rs.filter(r => Number.isFinite(r.lf));
    const burn = rs.filter(r => r.block_fuel != null && r.afad != null && r.block && r.block_fuel > r.afad);
    const burnSum = burn.reduce((s, r) => s + (r.block_fuel - r.afad), 0);
    return { t, rs, lf: lf.length ? Math.round((sumOf(lf, 'lf') / lf.length) * 100) : null, pax: sumOf(rs, 'pax'),
      burnN: burn.length, perFlight: burn.length ? Math.round(burnSum / burn.length) : null,
      perHour: burn.length ? Math.round(burnSum / (sumOf(burn, 'block') / 60)) : null };
  }).sort((a, b) => b.rs.length - a.rs.length);
  const fuel = rows.filter(r => r.plan_landing_fuel != null && r.afad != null);
  const diffs = fuel.map(r => r.afad - r.plan_landing_fuel);
  const bands = [['Under −500 kg', -Infinity, -501], ['−500 to −201', -500, -201], ['−200 to −1', -200, -1], ['0 to +200', 0, 200], ['+201 to +500', 201, 500], ['Over +500 kg', 501, Infinity]]
    .map(([label, lo, hi]) => ({ label, short: label.replace(' kg', ''), v: diffs.filter(d => d >= lo && d <= hi).length }));
  const avgDiff = diffs.length ? Math.round(diffs.reduce((s, d) => s + d, 0) / diffs.length) : null;
  const within = diffs.length ? pct(diffs.filter(d => Math.abs(d) <= 200).length, diffs.length) : null;
  const stat = (label, value, note) => `<div class="st-mini"><span>${label}</span><b>${value}</b>${note ? `<em>${note}</em>` : ''}</div>`;
  el.innerHTML = `
    <div class="st-grid st-grid-2">
      <section class="st-card st-card-scroll">
        <h3>LOAD</h3>
        <div class="st-minis">
          ${stat('Passengers carried', fmtNum(sumOf(rows, 'pax')))}
          ${stat('Average load factor', avgLf == null ? '&mdash;' : avgLf + '%', `${withLf.length} flights`)}
          ${stat('Average passengers', withLf.length ? fmtNum(Math.round(sumOf(rows, 'pax') / rows.filter(r => r.pax != null).length)) : '&mdash;', 'per flight')}
          ${stat('Cargo and bags', fmtNum(Math.round(sumOf(rows, 'cargo') / 1000)) + ' t')}
        </div>
        <h3>BY AIRCRAFT TYPE</h3>
        <div class="st-scroll"><table class="st-table st-clickable"><thead><tr><th>TYPE</th><th class="num">FLIGHTS</th><th class="num">PASSENGERS</th><th class="num">LOAD FACTOR</th><th class="num">BURN / FLIGHT</th><th class="num">BURN / HOUR</th></tr></thead><tbody>
          ${byType.map(x => `<tr onclick="setStatsFilter('type', '${escAttr(x.t)}')" tabindex="0" title="Filter by this"><td>${escText(typeName(x.t))}</td><td class="num">${x.rs.length}</td><td class="num">${fmtNum(x.pax)}</td><td class="num">${x.lf == null ? '&mdash;' : x.lf + '%'}</td>
            <td class="num">${x.perFlight == null ? '&mdash;' : fmtNum(x.perFlight) + ' kg'}</td><td class="num">${x.perHour == null ? '&mdash;' : fmtNum(x.perHour) + ' kg/h'}</td></tr>`).join('')}
        </tbody></table></div>
        <p class="st-hint">Burn is OFP block fuel minus your actual fuel at destination, over the flight time (off-block to in-block). It needs an OFP and a PIREP with AFAD</p>
      </section>
      <section class="st-card st-card-stack">
        <h3>FUEL AT DESTINATION: ACTUAL VS PLANNED</h3>
        ${fuel.length ? `
          <div class="st-minis">
            ${stat('Average difference', (avgDiff > 0 ? '+' : '') + fmtNum(avgDiff) + ' kg', avgDiff >= 0 ? 'landing with more than planned' : 'landing with less than planned')}
            ${stat('Within ±200 kg', within + '%', `${fuel.length} flights`)}
          </div>
          ${statsMiniCols(bands, v => `${v} ${v === 1 ? 'flight' : 'flights'}`)}
          <p class="st-hint">Your AFAD against the OFP's planned landing fuel. Negative means you landed with less fuel than planned</p>`
          : `<p class="st-hint">No flights with a planned landing fuel yet. The app started saving it from SimBrief with this release, so flights whose OFP you fetch from now on will appear here</p>`}
      </section>
    </div>`;
}

// -- Records --
function statsRecords(el, rows) {
  const by = (k, dir = -1) => rows.filter(r => r[k] != null).sort((a, b) => dir * (a[k] - b[k]))[0];
  const flightNote = r => r ? `${escText(r.fn)} &middot; ${r.dep} &rarr; ${r.arr} &middot; ${fmtDay(r.date)}` : '';
  const days = [...groupRows(rows, r => r.date)];
  const busiest = days.sort((a, b) => b[1].length - a[1].length || sumOf(b[1], 'ft') - sumOf(a[1], 'ft'))[0];
  const longestDay = [...days].sort((a, b) => sumOf(b[1], 'ft') - sumOf(a[1], 'ft'))[0];
  const dates = [...new Set(rows.map(r => r.date).filter(Boolean))].sort();
  let best = { n: 0 }, cur = { n: 0 };
  dates.forEach((d, i) => {
    cur = i && daysBetween(dates[i - 1], d) === 1 ? { n: cur.n + 1, from: cur.from, to: d } : { n: 1, from: d, to: d };
    if (cur.n > best.n) best = Object.assign({}, cur);
  });
  const top = (m) => [...m].sort((a, b) => b[1].length - a[1].length)[0];
  const route = top(groupRows(rows, r => [r.dep, r.arr].sort().join('-')));
  const moves = new Map(); rows.forEach(r => [r.dep, r.arr].forEach(a => moves.set(a, (moves.get(a) || 0) + 1)));
  const ap = [...moves].sort((a, b) => b[1] - a[1])[0];
  const type = top(groupRows(rows, r => r.type));
  const lateR = by('dep_delay'), fullR = rows.filter(r => r.pax != null).sort((a, b) => (b.lf || 0) - (a.lf || 0))[0];
  const card = (label, value, note) => value == null ? '' : `<div class="st-record"><span>${label}</span><b>${value}</b><em>${note || ''}</em></div>`;
  el.innerHTML = `<section class="st-card st-card-scroll"><div class="st-scroll"><div class="st-records">
    ${card('Longest flight', by('dist') ? fmtNum(by('dist').dist) + ' nm' : null, flightNote(by('dist')))}
    ${card('Longest flight time', by('ft') ? fmtHours(by('ft').ft) : null, flightNote(by('ft')))}
    ${card('Shortest flight', by('dist', 1) ? fmtNum(by('dist', 1).dist) + ' nm' : null, flightNote(by('dist', 1)))}
    ${card('Busiest day', busiest ? `${busiest[1].length} ${busiest[1].length === 1 ? 'flight' : 'flights'}` : null, busiest ? `${fmtDay(busiest[0], { weekday: 'short' })} &middot; ${fmtHours(sumOf(busiest[1], 'ft'))}` : '')}
    ${card('Most flying in a day', longestDay ? fmtHours(sumOf(longestDay[1], 'ft')) : null, longestDay ? fmtDay(longestDay[0], { weekday: 'short' }) : '')}
    ${card('Longest streak', best.n ? `${best.n} ${best.n === 1 ? 'day' : 'days'} in a row` : null, best.n ? `${fmtDay(best.from)} &ndash; ${fmtDay(best.to)}` : '')}
    ${card('Most flown route', route ? `${route[0].replace('-', ' &ndash; ')}` : null, route ? `${route[1].length} ${route[1].length === 1 ? 'flight' : 'flights'}` : '')}
    ${card('Most visited airport', ap ? `${ap[0]}` : null, ap ? `${escText(statsAirport(ap[0]).name || '')} &middot; ${ap[1]} movements` : '')}
    ${card('Most flown aircraft', type ? escText(typeName(type[0])) : null, type ? `${type[1].length} flights &middot; ${fmtHours(sumOf(type[1], 'ft'))}` : '')}
    ${card('Biggest departure delay', lateR ? fmtSigned(lateR.dep_delay) : null, flightNote(lateR))}
    ${card('Fullest flight', fullR && fullR.lf != null ? Math.round(fullR.lf * 100) + '%' : null, fullR ? `${fmtNum(fullR.pax)} of ${fmtNum(fullR.seats)} seats &middot; ${flightNote(fullR)}` : '')}
    ${card('First flight in range', rows.length ? fmtDay(rows[0].date) : null, flightNote(rows[0]))}
  </div></div></section>`;
}

// -- Logbook --
const LOGBOOK_COLUMNS = [
  ['date', 'DATE', r => fmtDay(r.date), r => r.date],
  ['fn', 'FLIGHT', r => escText(r.fn), r => r.fn],
  ['cs', 'CALLSIGN', r => escText(r.cs || ''), r => r.cs],
  ['carrier', 'AIRLINE', r => escText(CARRIER_NAMES[r.carrier] || r.carrier || ''), r => r.carrier],
  ['type', 'AIRCRAFT', r => escText(typeName(r.type)), r => r.type],
  ['dep', 'FROM', r => r.dep, r => r.dep],
  ['arr', 'TO', r => r.arr, r => r.arr],
  ['dist', 'DISTANCE', r => r.dist != null ? fmtNum(r.dist) + ' nm' : '&mdash;', r => r.dist],
  ['ft', 'FLIGHT TIME', r => r.ft != null ? fmtHours(r.ft) + (r.ft_est ? '*' : '') : '&mdash;', r => r.ft],
  ['air', 'AIR TIME', r => r.air != null ? fmtHours(r.air) : '&mdash;', r => r.air],
  ['dep_delay', 'DEP DELAY', r => fmtSigned(r.dep_delay), r => r.dep_delay],
  ['pax', 'PAX', r => r.pax != null ? fmtNum(r.pax) : '&mdash;', r => r.pax],
];

function statsLogbook(el, rows) {
  const s = statsState.sort, col = LOGBOOK_COLUMNS.find(c => c[0] === s.key) || LOGBOOK_COLUMNS[0];
  const sorted = [...rows].sort((a, b) => {
    const x = col[3](a), y = col[3](b);
    if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
    const c = typeof x === 'number' ? x - y : String(x).localeCompare(String(y));
    return s.dir === 'asc' ? c : -c;
  });
  el.innerHTML = `
    <section class="st-card st-card-scroll st-logbook">
      <div class="st-card-head"><h3>LOGBOOK <span class="st-muted">${rows.length} ${rows.length === 1 ? 'flight' : 'flights'}</span></h3>
        <button class="action small" type="button" onclick="exportStatsCsv()">EXPORT CSV</button></div>
      <div class="st-scroll"><table class="st-table st-log"><thead><tr>${LOGBOOK_COLUMNS.map(([k, l]) =>
        `<th class="sortable${k === s.key ? ' sort-active' : ''}${['dist', 'ft', 'air', 'dep_delay', 'pax'].includes(k) ? ' num' : ''}" onclick="sortStatsLog('${k}')" aria-sort="${k === s.key ? (s.dir === 'asc' ? 'ascending' : 'descending') : 'none'}">${l}<span class="sort-arrow">${k === s.key ? (s.dir === 'asc' ? '&uarr;' : '&darr;') : '&uarr;'}</span></th>`).join('')}</tr></thead>
        <tbody>${sorted.map(r => `<tr>${LOGBOOK_COLUMNS.map(([k, , show]) => `<td class="${['dist', 'ft', 'air', 'dep_delay', 'pax'].includes(k) ? 'num' : ''}">${show(r)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
      ${rows.some(r => r.ft_est) ? '<p class="st-hint">* Estimated: this PIREP has no actual block times, so its scheduled block time is counted</p>' : ''}
    </section>`;
}

function sortStatsLog(key) {
  const s = statsState.sort;
  if (s.key === key) s.dir = s.dir === 'asc' ? 'desc' : 'asc'; else { s.key = key; s.dir = key === 'date' ? 'desc' : 'asc'; }
  renderStatsBody();
}

function exportStatsCsv() {
  const range = statsRange();
  const rows = statsData.flights.filter(r => rowMatches(r, range, statsState.f));
  const cols = [['date', 'Date'], ['fn', 'Flight'], ['cs', 'Callsign'], ['carrier', 'Airline'], ['op', 'Operator'], ['type', 'Aircraft type'],
    ['dep', 'From'], ['arr', 'To'], ['dist', 'Distance (nm)'], ['ft', 'Flight time (min)'], ['ft_est', 'Flight time estimated'], ['air', 'Air time (min)'],
    ['sched_block', 'Scheduled block (min)'], ['dep_delay', 'Departure delay (min)'], ['arr_delay', 'Arrival delay (min)'], ['pax', 'Passengers'], ['seats', 'Seats'],
    ['lf', 'Load factor'], ['cargo', 'Cargo (kg)'], ['ci', 'Cost index'], ['delay_code', 'Delay code'], ['delay_desc', 'Delay reason'], ['mel', 'MEL'],
    ['block_fuel', 'Block fuel (kg)'], ['plan_trip_fuel', 'Planned trip fuel (kg)'], ['plan_landing_fuel', 'Planned landing fuel (kg)'], ['afad', 'Actual fuel at destination (kg)']];
  const cell = v => { if (v == null) return ''; const t = String(v); return /[",\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t; };
  const csv = [cols.map(c => c[1]).join(','), ...rows.map(r => cols.map(([k]) => cell(r[k])).join(','))].join('\n');
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  const a = document.createElement('a');
  a.href = url; a.download = `virtualdispatch-logbook-${range[0]}-to-${range[1]}.csv`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

let statsResizeRaf = 0;
window.addEventListener('resize', () => {
  if (statsResizeRaf || !document.getElementById('stTimeline') || statsState.timelineTable) return;
  statsResizeRaf = requestAnimationFrame(() => { statsResizeRaf = 0; renderStatsBody(); });
});

// Last, so every top-level let/const above is initialized before the
// first page renders (the search page reads several synchronously).
boot();
