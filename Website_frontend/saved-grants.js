/* ═══════════════════════════════════════
   SAVED PORTFOLIO — Grants panel of saved.html.
   Ids/functions are namespaced (Grant-suffixed) because saved.html loads
   this alongside saved-tenders.js on the same page.
   ════════════════════════════════════════ */

// Authoritative saved-grants list, loaded from saved_grants.xlsx via the
// backend. The Excel file (written server-side on every star click) is the
// single source of truth for "what's saved" — not localStorage.
window.SensioGrantSavedData = { grants: [] };

document.addEventListener('DOMContentLoaded', async () => {
  await loadSavedGrantsFromServer();
  initializeSavedGrantCountryDropdown();
  renderSavedGrants();
  registerSavedGrantListeners();
});

async function loadSavedGrantsFromServer() {
  const endpoint = `${SENSIO_API_BASE}/api/grants/saved-grants`;

  try {
    const response = await fetch(endpoint, {
      method: 'GET',
      mode: 'cors',
      headers: { 'Accept': 'application/json' }
    });

    if (!response.ok) {
      throw new Error(`Server returned invalid status code: ${response.status}`);
    }

    const payload = await response.json();
    window.SensioGrantSavedData.grants = Array.isArray(payload.grants) ? payload.grants : [];

    // Merge into the shared dataset (dedup by id) so openGrantModal() in
    // grants-ui.js keeps working for saved items even if a grant has since
    // fallen out of the live scraper sweep.
    if (window.SensioGrantData) {
      await window.SensioGrantData.ready;
      const existingIds = new Set(window.SensioGrantData.grants.map(t => t.id));
      window.SensioGrantSavedData.grants.forEach(t => {
        if (!existingIds.has(t.id)) window.SensioGrantData.grants.push(t);
      });
    }

    console.log(`⭐ Loaded ${window.SensioGrantSavedData.grants.length} saved grant(s) from saved_grants.xlsx.`);
  } catch (err) {
    console.error('[Saved Grants]: Could not reach backend. Ensure server.py is running!', err);
    window.SensioGrantSavedData.grants = [];
  }
}

function initializeSavedGrantCountryDropdown() {
  const select = document.getElementById('savedGrantCountrySelector');
  if (!select) return;

  const dataset = window.SensioGrantSavedData.grants;
  const uniqueCountries = [...new Set(dataset.map(item => item.country).filter(Boolean))].sort();

  select.innerHTML = '<option value="all">🌍 All Countries</option>';
  uniqueCountries.forEach(country => {
    const opt = document.createElement('option');
    opt.value = country.toLowerCase();
    opt.textContent = country;
    select.appendChild(opt);
  });
}

function registerSavedGrantListeners() {
  const elements = ['savedGrantSearchInput', 'savedGrantCountrySelector', 'savedGrantSectorSelector'];
  elements.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', renderSavedGrants);
  });
}

// "Starred By" comes from the backend as a comma-joined string of founder names
// (derived from which per-founder saved_<name>.xlsx files hold the grant).
function parseGrantStarredBy(item) {
  return String(item.starredBy || '')
    .split(',')
    .map(s => s.trim())
    .filter(Boolean);
}

function renderSavedGrants() {
  const container = document.getElementById('savedGrantGridDisplay');
  if (!container) return;

  const searchVal  = document.getElementById('savedGrantSearchInput')?.value.toLowerCase() || '';
  const countryVal = document.getElementById('savedGrantCountrySelector')?.value || 'all';
  const sectorVal  = document.getElementById('savedGrantSectorSelector')?.value || 'all';

  // Apply the shared filters once, then split into per-founder sections below.
  let filtered = [...window.SensioGrantSavedData.grants];

  if (searchVal.trim() !== '') {
    filtered = filtered.filter(item =>
      (item.title || '').toLowerCase().includes(searchVal) ||
      (item.description || '').toLowerCase().includes(searchVal)
    );
  }
  if (countryVal !== 'all') {
    filtered = filtered.filter(item => (item.country || '').toLowerCase() === countryVal);
  }
  if (sectorVal !== 'all') {
    filtered = filtered.filter(item => (item.category || '').toLowerCase() === sectorVal);
  }

  // Counter = number of distinct saved grants (a grant saved by two founders
  // still counts once here, even though it appears in both sections).
  const counter = document.getElementById('savedGrantCounterMetric');
  if (counter) counter.textContent = filtered.length;

  // Section order: the known founders first, then any other name that shows up
  // in the data (e.g. a legacy "unknown" bucket) so nothing is ever hidden.
  const known = (typeof FOUNDER_NAMES !== 'undefined' && Array.isArray(FOUNDER_NAMES)) ? FOUNDER_NAMES : [];
  const extra = [];
  filtered.forEach(item => parseGrantStarredBy(item).forEach(n => {
    if (!known.includes(n) && !extra.includes(n)) extra.push(n);
  }));
  const founders = [...known, ...extra];

  if (founders.length === 0) {
    container.innerHTML = `<div style="text-align:center; padding:48px; color:var(--text-muted);">No saved grants yet. Bookmark items from the dashboard or Grants tab.</div>`;
    return;
  }

  container.innerHTML = founders.map(founder => {
    const mine = filtered.filter(item => parseGrantStarredBy(item).includes(founder));
    const body = mine.length
      ? `<div class="grant-grid">${mine.map(item => grantSavedCardHtml(item, founder)).join('')}</div>`
      : `<div style="padding:18px 4px; color:var(--text-muted); font-size:14px;">No grants saved by ${founder} yet.</div>`;
    return `
      <section class="saved-founder-section" style="margin-bottom:34px;">
        <div style="display:flex; align-items:center; gap:10px; margin-bottom:14px; padding-bottom:8px; border-bottom:1px solid var(--border-color, #e5e7eb);">
          <span style="width:28px; height:28px; border-radius:50%; background:var(--color-blue, #0284c7); color:#fff; display:inline-flex; align-items:center; justify-content:center; font-weight:700; font-size:13px;">${founder.charAt(0).toUpperCase()}</span>
          <h2 style="font-size:16px; margin:0;">${founder}</h2>
          <span style="font-size:13px; color:var(--text-muted); font-weight:600;">${mine.length} saved</span>
        </div>
        ${body}
      </section>
    `;
  }).join('');
}

function grantSavedCardHtml(item, founder) {
  const daysLeft = calculateDaysRemaining(item.closingDate);
  let badgeHtml = '';
  if (isNaN(daysLeft)) {
    badgeHtml = `<div class="urgency-badge" style="background-color:#f3f4f6; color:#6b7280;">No deadline</div>`;
  } else if (daysLeft < 0) {
    badgeHtml = `<div class="urgency-badge" style="background-color:#fee2e2; color:#ef4444;">Expired</div>`;
  } else if (daysLeft <= 7) {
    badgeHtml = `<div class="urgency-badge urgency-closing">⏰ Closing Soon (${daysLeft}d remaining)</div>`;
  } else {
    badgeHtml = `<div class="urgency-badge urgency-active">✅ Active (${daysLeft}d remaining)</div>`;
  }

  // Indicate when the same grant is on more than one founder's list.
  const others = parseGrantStarredBy(item).filter(n => n !== founder);
  const sharedHtml = others.length
    ? `<div class="shared-badge" title="Also saved by ${others.join(', ')}" style="display:inline-flex; align-items:center; gap:5px; margin-top:8px; padding:3px 9px; border-radius:999px; background:#eef2ff; color:#4f46e5; font-size:12px; font-weight:600;">👥 Shared · also saved by ${others.join(', ')}</div>`
    : '';

  return `
    <div class="grant-card fade-in-up">
      <button class="save-btn saved" onclick="unsaveGrantFromSavedPage('${item.id}', '${founder}', this)" title="Remove from ${founder}'s saved">⭐</button>
      <div>
        <h3 class="grant-card-title">${item.title}</h3>
        ${badgeHtml}
        ${sharedHtml}
        <div class="grant-card-dates">
          <div class="date-row"><span class="date-label">Country:</span><span class="date-val">🌍 ${item.country || '—'}</span></div>
          <div class="date-row"><span class="date-label">Opening Phase:</span><span class="date-val">${item.openingDate}</span></div>
          <div class="date-row"><span class="date-label">Deadline Phase:</span><span class="date-val">${item.closingDate}</span></div>
        </div>
      </div>
      <div class="relevancy-score-container">
        <span class="relevancy-label">Relevancy Core</span>
        <span class="relevancy-pill">${(item.relevancyScore * 100).toFixed(0)}%</span>
      </div>
      <button class="btn btn-ghost" onclick="openGrantModal('${item.id}')" style="margin-top:14px; width:100%; justify-content:center; font-size:13px;">View Specifications</button>
    </div>
  `;
}

/*
  Unsave waits for the server to confirm the row was actually updated in
  saved_grants.xlsx before touching the UI. If the request fails or gets
  cut off (e.g. by navigation), the card stays put and the button
  re-enables — so the UI can never silently drift ahead of the Excel file.
*/
function unsaveGrantFromSavedPage(grantId, founderName, buttonEl) {
  // The star lives inside a specific founder's section, so remove it from THAT
  // founder's list — not necessarily whoever is currently signed in.
  if (!founderName) {
    alert('Could not determine which founder to unsave for.');
    return;
  }
  if (buttonEl) buttonEl.disabled = true;

  fetch(`${SENSIO_API_BASE}/api/grants/save-grant`, {
    method: 'POST',
    mode: 'cors',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ grantId: grantId, isSaved: false, founderName: founderName })
  })
    .then(res => {
      if (!res.ok) throw new Error(`Server returned ${res.status}`);
      return res.json();
    })
    .then(async (data) => {
      console.log('[Saved Grants]: Removed for', founderName, '— saved count is now', data.savedCount);
      // Re-pull the merged list so a grant still saved by another founder stays
      // visible (in their section) rather than disappearing entirely.
      await loadSavedGrantsFromServer();
      renderSavedGrants();
      initializeSavedGrantCountryDropdown();
    })
    .catch(err => {
      console.error('[Saved Grants]: Failed to remove from backend.', err);
      alert('Could not remove this grant — the server did not confirm the change. Please try again.');
      if (buttonEl) buttonEl.disabled = false;
    });
}
