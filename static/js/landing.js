/* Landing page, "How it works": three small demos of the app on sample
   data - a route search whose map follows the filters, the briefing's
   status lights with what's behind each, a gate change that updates the
   loadsheet, and a filterable logbook. Nothing here talks to the server
   except for the map outline (static/data/landing_map.json). */
(function () {
  'use strict';

  var section = document.getElementById('how-it-works');
  if (!section) return;
  var MAP_URL = section.getAttribute('data-map-url');
  var SVG_NS = 'http://www.w3.org/2000/svg';

  // Repeatable pseudo-random numbers, so the sample data is the same on
  // every visit.
  function rng(seed) {
    return function () {
      seed |= 0; seed = seed + 0x6D2B79F5 | 0;
      var t = Math.imul(seed ^ seed >>> 15, 1 | seed);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }
  function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  function hm(min) { min = ((min % 1440) + 1440) % 1440; return String(Math.floor(min / 60)).padStart(2, '0') + ':' + String(min % 60).padStart(2, '0'); }
  function el(tag, attrs) {
    var e = document.createElementNS(SVG_NS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  var AIRLINES = {
    RYR: { name: 'Ryanair', prefix: 'FR', types: ['B738'] },
    EZY: { name: 'easyJet', prefix: 'U2', types: ['A320', 'A319', 'A321'] },
    WZZ: { name: 'Wizz Air', prefix: 'W6', types: ['A321', 'A320'] },
  };
  // Which airlines fly from each base in the demo, and where
  var NETWORK = {
    EGKK: { airlines: ['EZY', 'RYR', 'WZZ'], dests: ['LEMD', 'LEBL', 'LEPA', 'LEMG', 'LEAL', 'LEIB', 'LPPT', 'LPFR', 'LPPR', 'LFMN', 'LFLL', 'LIRF', 'LIMC', 'LIPZ', 'LICC', 'LIRN', 'LGAV', 'LGIR', 'LGRP', 'LMML', 'EDDB', 'EDDM', 'EHAM', 'LSGG', 'LOWW', 'LKPR', 'EPKK', 'EKCH', 'EGPH', 'EGAC', 'EIDW', 'EICK', 'LHBP', 'LROP', 'LBSF', 'LTFM', 'GMMX', 'EVRA'] },
    EIDW: { airlines: ['RYR'], dests: ['EGKK', 'EGGW', 'EGPH', 'EGNX', 'LEMD', 'LEBL', 'LEPA', 'LEMG', 'LEAL', 'LPPT', 'LPFR', 'LPPR', 'LFPG', 'LIRF', 'LIMC', 'LIPZ', 'LICC', 'LIRN', 'LGAV', 'LMML', 'EDDB', 'EDDH', 'EHAM', 'EBBR', 'LOWW', 'LKPR', 'EPKK', 'EPWA', 'EKCH', 'ESSA', 'LHBP', 'EINN', 'GMMX'] },
    EGGW: { airlines: ['EZY', 'WZZ', 'RYR'], dests: ['LEBL', 'LEPA', 'LEMG', 'LEAL', 'LPFR', 'LPPT', 'LFMN', 'LIMC', 'LIRF', 'LGAV', 'EDDB', 'EHAM', 'LSGG', 'LKPR', 'EPKK', 'EPWA', 'LHBP', 'LHDC', 'LROP', 'LBSF', 'LWSK', 'LYBE', 'LDZA', 'LZIB', 'EVRA', 'EYVI', 'EGPH', 'EGAC', 'EIDW', 'ENGM', 'EFHK'] },
    LHBP: { airlines: ['WZZ', 'RYR'], dests: ['EGKK', 'EGGW', 'EIDW', 'LEMD', 'LEBL', 'LEMG', 'LEAL', 'LPPT', 'LFPG', 'LFMN', 'LIRF', 'LIMC', 'LIPZ', 'LICC', 'LIRN', 'LGAV', 'LGIR', 'LGRP', 'LMML', 'EDDB', 'EDDM', 'EHAM', 'EBBR', 'LSGG', 'EKCH', 'ENGM', 'ESSA', 'EFHK', 'LTFM', 'LROP', 'LBSF', 'LHDC', 'GMMX'] },
  };

  var MAP = null;         // landing_map.json once loaded
  var ITINS = [];         // the sample timetable, built once the map is in

  function distanceNm(a, b) {
    var r = Math.PI / 180, dLat = (b.lat - a.lat) * r, dLon = (b.lon - a.lon) * r;
    var h = Math.sin(dLat / 2) * Math.sin(dLat / 2) + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return Math.round(2 * 3440 * Math.asin(Math.sqrt(h)));
  }

  function buildTimetable() {
    var rand = rng(7), out = [];
    Object.keys(NETWORK).forEach(function (base) {
      var net = NETWORK[base];
      net.dests.forEach(function (dest) {
        net.airlines.forEach(function (al, k) {
          if (rand() > (k === 0 ? 0.9 : 0.42)) return;          // not every airline flies every route
          var n = 1 + Math.floor(rand() * 3);
          var nm = distanceNm(MAP.airports[base], MAP.airports[dest]);
          for (var i = 0; i < n; i++) {
            var etd = 330 + Math.floor(rand() * 64) * 15;          // 05:30Z to 21:15Z
            var rt = rand() < 0.4;
            out.push({
              airline: al, fn: AIRLINES[al].prefix + (100 + Math.floor(rand() * 8800)), dep: base, dest: dest, etd: etd,
              legs: rt ? 2 : 1, nm: nm, eet: Math.round(nm / 7.4 + 22),
              domestic: MAP.airports[base].country === MAP.airports[dest].country,
            });
          }
        });
      });
    });
    return out;
  }

  // ---- 1. Route search: filters and a map that follows them ----
  var routeState = { dep: 'EGKK', airline: 'all', legs: 'all', country: 'all', time: 'all' };
  var TIME_WINDOWS = { all: [0, 1440], morning: [360, 720], afternoon: [720, 1080], evening: [1080, 1440] };
  var view = null, viewAnim = 0, lastPoints = null;

  function filteredItins() {
    var s = routeState, w = TIME_WINDOWS[s.time];
    return ITINS.filter(function (it) {
      return it.dep === s.dep && (s.airline === 'all' || it.airline === s.airline)
        && (s.legs === 'all' || (s.legs === 'rt' ? it.legs === 2 : it.legs === 1))
        && (s.country === 'all' || (s.country === 'dom' ? it.domestic : !it.domestic))
        && it.etd >= w[0] && it.etd < w[1];
    }).sort(function (a, b) { return a.etd - b.etd; });
  }

  // Fit the view to the departure and the destinations shown, in the
  // map box's own proportions, easing from the previous view.
  function fitView(svg, points) {
    var box = svg.getBoundingClientRect();
    var aspect = box.width && box.height ? box.width / box.height : 1.6;
    var xs = points.map(function (p) { return p.x; }), ys = points.map(function (p) { return p.y; });
    var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs), minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys);
    var w = Math.max(maxX - minX, 240) * 1.25, h = Math.max(maxY - minY, 180) * 1.25;
    if (w / h < aspect) w = h * aspect; else h = w / aspect;
    var target = [(minX + maxX) / 2 - w / 2, (minY + maxY) / 2 - h / 2, w, h];
    var from = view || target, start = performance.now();
    var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    cancelAnimationFrame(viewAnim);
    function step(now) {
      var t = reduce ? 1 : Math.min(1, (now - start) / 600), e = 1 - Math.pow(1 - t, 3);
      view = from.map(function (v, i) { return v + (target[i] - v) * e; });
      svg.setAttribute('viewBox', view.map(function (v) { return v.toFixed(1); }).join(' '));
      // Markers and labels keep their on-screen size at every zoom
      svg.style.setProperty('--k', (view[2] / (box.width || 1000)).toFixed(4));
      if (t < 1) viewAnim = requestAnimationFrame(step);
    }
    viewAnim = requestAnimationFrame(step);
  }

  function arc(a, b) {
    var mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, dx = b.x - a.x, dy = b.y - a.y;
    var len = Math.sqrt(dx * dx + dy * dy) || 1, bend = Math.min(60, len * 0.18);
    return 'M' + a.x + ',' + a.y + ' Q' + (mx + dy / len * bend).toFixed(1) + ',' + (my - dx / len * bend).toFixed(1) + ' ' + b.x + ',' + b.y;
  }

  function renderRoutes() {
    var svg = document.getElementById('demoMap');
    var items = filteredItins();
    var base = MAP.airports[routeState.dep];
    var dests = {};
    items.forEach(function (it) { (dests[it.dest] = dests[it.dest] || []).push(it); });
    var routes = svg.querySelector('.dm-routes'), pts = svg.querySelector('.dm-points');
    routes.textContent = ''; pts.textContent = '';
    Object.keys(dests).forEach(function (icao, i) {
      var a = MAP.airports[icao];
      var path = el('path', { d: arc(base, a), class: 'dm-route', 'data-icao': icao, style: 'animation-delay:' + Math.min(i * 12, 400) + 'ms' });
      routes.appendChild(path);
      var g = el('g', { class: 'dm-dest', 'data-icao': icao, transform: 'translate(' + a.x + ',' + a.y + ')' });
      var m = el('g', { class: 'dm-mark' });
      m.appendChild(el('circle', { r: 4.5 }));
      var t = el('text', { x: 8, y: 4 }); t.textContent = a.city; m.appendChild(t);
      g.appendChild(m);
      pts.appendChild(g);
    });
    var bg = el('g', { class: 'dm-base', transform: 'translate(' + base.x + ',' + base.y + ')' });
    var bm = el('g', { class: 'dm-mark' });
    bm.appendChild(el('circle', { r: 11, class: 'dm-halo' }));
    bm.appendChild(el('circle', { r: 6.5 }));
    var bt = el('text', { x: 12, y: -10 }); bt.textContent = routeState.dep + ' · ' + base.city; bm.appendChild(bt);
    bg.appendChild(bm);
    pts.appendChild(bg);
    lastPoints = [base].concat(Object.keys(dests).map(function (k) { return MAP.airports[k]; }));
    fitView(svg, lastPoints);

    document.getElementById('demoCount').textContent = items.length;
    document.getElementById('demoDests').textContent = Object.keys(dests).length;
    var list = document.getElementById('demoList');
    list.innerHTML = items.length ? items.slice(0, 5).map(function (it) {
      var d = MAP.airports[it.dest];
      return '<li data-icao="' + it.dest + '"><span class="dl-al">' + it.airline + '</span><span class="dl-fn">' + it.fn + '</span>'
        + '<span class="dl-route">' + it.dep + ' &rarr; ' + it.dest + ' <small>' + esc(d.city) + '</small></span>'
        + '<span class="dl-time">' + hm(it.etd) + 'Z' + (it.legs === 2 ? ' &middot; RT' : '') + '</span></li>';
    }).join('') + (items.length > 5 ? '<li class="dl-more">+ ' + (items.length - 5) + ' more</li>' : '')
      : '<li class="dl-more">No flights match: try another filter</li>';
  }

  function highlight(icao) {
    document.querySelectorAll('#demoMap [data-icao]').forEach(function (n) {
      n.classList.toggle('hl', n.getAttribute('data-icao') === icao);
    });
    var svg = document.getElementById('demoMap');
    svg.classList.toggle('has-hl', !!icao);
  }

  function initRoutes() {
    var svg = document.getElementById('demoMap');
    svg.appendChild(el('path', { d: MAP.land, class: 'dm-land' }));
    svg.appendChild(el('g', { class: 'dm-routes' }));
    svg.appendChild(el('g', { class: 'dm-points' }));
    ITINS = buildTimetable();
    document.querySelectorAll('#demoFilters select').forEach(function (s) {
      s.addEventListener('change', function () { routeState[s.name] = s.value; renderRoutes(); });
    });
    var list = document.getElementById('demoList');
    list.addEventListener('mouseover', function (e) { var li = e.target.closest('li[data-icao]'); highlight(li ? li.getAttribute('data-icao') : null); });
    list.addEventListener('mouseleave', function () { highlight(null); });
    svg.addEventListener('mouseover', function (e) { var g = e.target.closest('.dm-dest'); highlight(g ? g.getAttribute('data-icao') : null); });
    svg.addEventListener('mouseleave', function () { highlight(null); });
    renderRoutes();
    // A new window size only refits the view; the routes stay as drawn
    var resizeRaf = 0;
    window.addEventListener('resize', function () {
      cancelAnimationFrame(resizeRaf);
      resizeRaf = requestAnimationFrame(function () { view = null; fitView(svg, lastPoints); });
    });
  }

  // ---- 2. Know before you go: status lights, each with its story ----
  var LEGS = [
    { note: 'U2803 London Gatwick to Belfast City: a late inbound and a generator deferred',
      lights: [
        ['ok', 'GO', '<ul class="ld-checks"><li class="ok">Weather at Gatwick and Belfast City above minima</li><li class="ok">Crew duty: 3h 20m planned of an 11h 30m limit</li><li class="ok">Belfast City curfew 22:00Z: 2h 40m margin</li></ul>'],
        ['warn', '+40 MIN', '<p><span class="ld-tag">IATA 93</span> Late arrival of the inbound aircraft</p><p class="ld-sub">Off-blocks 17:50Z instead of 17:10Z. The delay carries on to the next leg of the trip, and you code it in your PIREP</p>'],
        ['warn', '1 MEL', '<p><span class="ld-tag">MEL 24-21-01 &middot; CAT C</span> Engine generator 2 inoperative</p><ul class="ld-list"><li>Keep the APU running with its generator online for the whole flight</li><li>5 minutes of extra fuel for the APU burn, already in the SimBrief plan</li></ul>'],
        ['ok', 'CLEAR', '<p>No major or significant NOTAMs live at either end</p><p class="ld-sub">3 other NOTAMs in the briefing: taxiway works, stand closures, a crane near the approach</p>'],
      ] },
    { note: 'FR113 Dublin to London Gatwick: thunderstorms forecast, and the weather radar is out',
      lights: [
        ['bad', 'NO-GO', '<p><span class="ld-tag bad">NO-GO</span> Weather radar inoperative with thunderstorms forecast at Gatwick</p><p class="ld-sub">The MEL doesn\'t allow this departure. Swap the aircraft, or wait for the storms to clear</p>'],
        ['warn', '+25 SLOT', '<p><span class="ld-tag">IATA 81</span> Air traffic flow restriction over the UK</p><p class="ld-sub">Slot 07:20Z: be ready to push 15 minutes before it</p>'],
        ['bad', 'WX RADAR', '<p><span class="ld-tag bad">MEL 34-43-01</span> Weather radar inoperative</p><ul class="ld-list"><li>Dispatch only with no thunderstorms forecast along the route or at the destination</li></ul>'],
        ['warn', '1 MAJOR', '<p><span class="ld-tag">EGKK</span> Runway 08R/26L closed 0600-1400Z</p><p class="ld-sub">Single-runway operations: expect holding on arrival</p>'],
      ] },
    { note: 'W6 2202 London Luton to Budapest: frost on the wings and a runway closed at the destination',
      lights: [
        ['ok', 'GO', '<ul class="ld-checks"><li class="ok">Weather above minima at both ends</li><li class="ok">Crew duty: 2h 35m planned of a 12h 15m limit</li><li class="ok">No curfew at Budapest</li></ul>'],
        ['warn', '+18 MIN', '<p><span class="ld-tag">IATA 75</span> De-icing of the aircraft</p><p class="ld-sub">Freezing fog at Luton: the de-icing queue takes 18 minutes</p>'],
        ['ok', 'CLEAN', '<p>No deferred defects or missing panels</p>'],
        ['warn', 'RWY CLSD', '<p><span class="ld-tag">LHBP</span> Runway 13R/31L closed for maintenance</p><p class="ld-sub">Landing distance on the remaining runway is fine for the A321, plan the longer taxi</p>'],
      ] },
    { note: 'U2441 London Gatwick to Malaga: a quiet day',
      lights: [
        ['ok', 'GO', '<ul class="ld-checks"><li class="ok">Weather above minima at both ends</li><li class="ok">Crew duty: 2h 40m planned of a 13h limit</li></ul>'],
        ['ok', 'ON TIME', '<p>No delay expected: push on schedule at 08:25Z</p>'],
        ['ok', 'CLEAN', '<p>No deferred defects or missing panels</p>'],
        ['ok', 'CLEAR', '<p>No major or significant NOTAMs live at either end</p>'],
      ] },
  ];
  var legIndex = 0, lightIndex = 1;

  function renderLights(animate) {
    var leg = LEGS[legIndex];
    var buttons = document.querySelectorAll('#demoLights .light');
    buttons.forEach(function (b, i) {
      var set = function () {
        b.className = 'light ' + leg.lights[i][0] + (i === lightIndex ? ' on' : '');
        b.querySelector('span').textContent = leg.lights[i][1];
        b.setAttribute('aria-selected', i === lightIndex ? 'true' : 'false');
      };
      if (animate) {
        b.className = 'light rolling'; b.querySelector('span').textContent = '· · ·';
        setTimeout(set, 220 + i * 170);
      } else set();
    });
    var detail = document.getElementById('demoLightDetail');
    var fill = function () {
      detail.innerHTML = '<b class="ld-k">' + buttons[lightIndex].querySelector('b').textContent + '</b>' + leg.lights[lightIndex][2];
      document.getElementById('demoLegNote').textContent = leg.note;
    };
    if (animate) setTimeout(fill, 900); else fill();
  }

  function initLights() {
    document.querySelectorAll('#demoLights .light').forEach(function (b, i) {
      b.addEventListener('click', function () { lightIndex = i; renderLights(false); });
    });
    document.getElementById('demoRoll').addEventListener('click', function () {
      legIndex = (legIndex + 1) % LEGS.length;
      lightIndex = LEGS[legIndex].lights.findIndex(function (l) { return l[0] !== 'ok'; });
      if (lightIndex < 0) lightIndex = 0;
      renderLights(true);
    });
    renderLights(false);
  }

  // ---- 3. At the gate: a last-minute change and the loadsheet ----
  var GATE = [
    { text: 'Extra bags checked in at the gate: <b>+60 kg in the hold</b>', kg: 60, line: 'EXTRA BAGS        +60' },
    { text: 'Two late passengers made it to the gate: <b>+2 passengers</b>', kg: 168, line: 'PAX 2 ADULTS     +168' },
    { text: 'Dry ice for perishable cargo: <b>UN1845, 40 kg in hold 1</b>. Accept the NOTOC, or offload it', kg: 40, line: 'NOTOC UN1845      +40' },
  ];
  var gateIndex = 0;
  function loadsheet(edition, extra, line) {
    return '<span class="w">LOADSHEET  FINAL     EDNO ' + edition + '</span>\n'
      + '<span class="c">EGKK/EGAC U2803  G-EZWX</span>\n'
      + 'ZERO FUEL WT  ' + (58190 + extra) + ' <span class="c">MAX 62500</span>\n'
      + 'TAKE OFF WT   ' + (64390 + extra) + ' <span class="c">MAX 73500</span>\n'
      + 'LANDING WT    ' + (61490 + extra) + ' <span class="c">MAX 64500</span>\n'
      + (line ? '<span class="a">LMC ' + line + '</span>' : 'NO LAST MINUTE CHANGES');
  }
  function renderGate(decision) {
    var g = GATE[gateIndex];
    document.getElementById('demoLmcText').innerHTML = g.text;
    var acts = document.getElementById('demoLmcActs'), done = document.getElementById('demoLmcDone');
    acts.hidden = !!decision; done.hidden = !decision;
    if (decision) done.innerHTML = decision === 'accept'
      ? '<span class="ld-ok">Accepted: loadsheet edition 02 issued</span>'
      : '<span class="ld-ok">Declined: offloaded, edition 01 stands</span>';
    document.getElementById('demoLmcCard').classList.toggle('done', !!decision);
    document.getElementById('demoSheet').innerHTML = decision === 'accept' ? loadsheet('02', g.kg, g.line) : loadsheet('01', 0, '');
  }
  function initGate() {
    document.getElementById('demoAccept').addEventListener('click', function () { renderGate('accept'); });
    document.getElementById('demoDecline').addEventListener('click', function () { renderGate('decline'); });
    document.getElementById('demoNextLmc').addEventListener('click', function () { gateIndex = (gateIndex + 1) % GATE.length; renderGate(null); });
    renderGate(null);
  }

  // ---- 4. Logbook: a sample pilot's flights, with the stats filters ----
  var LOG = [], statState = { range: '365', airline: 'all', type: 'all' };
  function buildLog() {
    var rand = rng(42), today = new Date(), out = [];
    var routes = ITINS.filter(function (it) { return it.dep === 'EGKK' || it.dep === 'EGGW'; });
    for (var day = 730; day >= 1; day--) {
      if (rand() > 0.3) continue;
      var it = routes[Math.floor(rand() * routes.length)];
      var types = AIRLINES[it.airline].types;
      var late = rand() < 0.3 ? Math.round(5 + rand() * 50) : 0;
      var date = new Date(today.getTime() - day * 86400000);
      [[it.dep, it.dest], [it.dest, it.dep]].forEach(function (pair) {
        out.push({ date: date, airline: it.airline, type: types[Math.floor(rand() * types.length)], dep: pair[0], arr: pair[1],
          block: it.eet + 12, nm: it.nm, delay: late, domestic: it.domestic });
      });
    }
    return out;
  }
  function inRange(rows) {
    var days = statState.range === 'all' ? Infinity : Number(statState.range), now = Date.now();
    return rows.filter(function (r) {
      return (now - r.date.getTime()) / 86400000 <= days && (statState.airline === 'all' || r.airline === statState.airline)
        && (statState.type === 'all' || r.type === statState.type);
    });
  }
  function renderStats() {
    var rows = inRange(LOG);
    var mins = rows.reduce(function (s, r) { return s + r.block; }, 0);
    var nm = rows.reduce(function (s, r) { return s + r.nm; }, 0);
    var onTime = rows.filter(function (r) { return r.delay < 15; }).length;
    var delayed = rows.filter(function (r) { return r.delay > 0; });
    var kpis = [
      ['Flights', rows.length],
      ['Block time', Math.floor(mins / 60) + 'h ' + String(mins % 60).padStart(2, '0') + 'm'],
      ['Distance', nm.toLocaleString('en-GB') + ' nm'],
      ['On-time departures', rows.length ? Math.round(onTime / rows.length * 100) + '%' : '—'],
      ['Average delay', delayed.length ? Math.round(delayed.reduce(function (s, r) { return s + r.delay; }, 0) / delayed.length) + ' min' : '—'],
    ];
    document.getElementById('demoKpis').innerHTML = kpis.map(function (k) { return '<div class="kpi"><span>' + k[0] + '</span><b>' + k[1] + '</b></div>'; }).join('');

    // Flights per week for short ranges, per month otherwise
    var weekly = statState.range !== 'all' && Number(statState.range) <= 90;
    var buckets = [], keyOf, labelOf, now = new Date();
    if (weekly) {
      var weeks = Math.ceil(Number(statState.range) / 7);
      for (var w = weeks - 1; w >= 0; w--) buckets.push({ key: w, n: 0, label: new Date(now.getTime() - w * 7 * 86400000).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) });
      keyOf = function (r) { return Math.floor((now - r.date) / (7 * 86400000)); };
    } else {
      var months = statState.range === 'all' ? 24 : 12;
      for (var m = months - 1; m >= 0; m--) {
        var d = new Date(now.getFullYear(), now.getMonth() - m, 1);
        buckets.push({ key: d.getFullYear() * 12 + d.getMonth(), n: 0, label: d.toLocaleDateString('en-GB', { month: 'short' }) + (d.getMonth() === 0 || m === months - 1 ? ' ' + String(d.getFullYear()).slice(2) : '') });
      }
      keyOf = function (r) { return r.date.getFullYear() * 12 + r.date.getMonth(); };
    }
    labelOf = {}; buckets.forEach(function (b) { labelOf[b.key] = b; });
    rows.forEach(function (r) { var b = labelOf[keyOf(r)]; if (b) b.n++; });
    var max = Math.max.apply(null, buckets.map(function (b) { return b.n; }).concat([1]));
    var every = Math.ceil(buckets.length / 8);
    document.getElementById('demoBars').innerHTML = buckets.map(function (b, i) {
      return '<div class="bar" title="' + b.label + ': ' + b.n + ' flight' + (b.n === 1 ? '' : 's') + '"><i style="height:' + Math.max(2, b.n / max * 100) + '%"></i><em>' + b.n + '</em><small>' + (i % every === 0 ? b.label : '') + '</small></div>';
    }).join('');
    document.getElementById('demoBarsTitle').textContent = weekly ? 'Flights by week' : 'Flights by month';

    var count = {};
    rows.forEach(function (r) { [r.dep, r.arr].forEach(function (a) { count[a] = (count[a] || 0) + 1; }); });
    var top = Object.keys(count).sort(function (a, b) { return count[b] - count[a]; }).slice(0, 5);
    var topMax = top.length ? count[top[0]] : 1;
    document.getElementById('demoTop').innerHTML = top.length ? top.map(function (a) {
      return '<li><span>' + a + ' <small>' + esc(MAP.airports[a].city) + '</small></span><i><b style="width:' + (count[a] / topMax * 100) + '%"></b></i><em>' + count[a] + '</em></li>';
    }).join('') : '<li class="dl-more">No flights in this selection</li>';
    var dom = rows.filter(function (r) { return r.domestic; }).length;
    var domPct = rows.length ? Math.round(dom / rows.length * 100) : 0;
    document.getElementById('demoSplit').innerHTML = '<div class="split"><i style="width:' + domPct + '%"></i><b style="width:' + (100 - domPct) + '%"></b></div>'
      + '<p><span class="sw-d"></span>Domestic ' + dom + ' (' + domPct + '%) <span class="sw-i"></span>International ' + (rows.length - dom) + ' (' + (100 - domPct) + '%)</p>';
  }
  function initStats() {
    LOG = buildLog();
    document.querySelectorAll('#demoRanges button').forEach(function (b) {
      b.addEventListener('click', function () {
        statState.range = b.getAttribute('data-range');
        document.querySelectorAll('#demoRanges button').forEach(function (x) { x.classList.toggle('on', x === b); x.setAttribute('aria-pressed', x === b ? 'true' : 'false'); });
        renderStats();
      });
    });
    document.querySelectorAll('#demoStatFilters select').forEach(function (s) {
      s.addEventListener('change', function () { statState[s.name] = s.value; renderStats(); });
    });
    renderStats();
  }

  // Everything starts once the section is close to the screen, so the map
  // outline is only downloaded by visitors who scroll this far.
  var started = false;
  function start() {
    if (started) return;
    started = true;
    initLights();
    initGate();
    fetch(MAP_URL).then(function (r) { return r.json(); }).then(function (data) {
      MAP = data;
      initRoutes();
      initStats();
      section.classList.add('is-ready');
    }).catch(function () { section.classList.add('map-failed'); });
  }
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      if (entries.some(function (e) { return e.isIntersecting; })) { io.disconnect(); start(); }
    }, { rootMargin: '600px 0px' });
    io.observe(section);
  } else start();
})();
