/* ═══════════════════════════════════════
   SAVED PORTFOLIO — Tenders panel of saved.html.
   Ids/functions are namespaced (Tender-suffixed) because saved.html loads
   this alongside saved-grants.js on the same page.
   ════════════════════════════════════════ */

// Authoritative saved-tenders list, loaded from saved_tenders.xlsx via the
// backend. The Excel file (written server-side on every star click) is the
// single source of truth for "what's saved" — not localStorage.
window.SensioTenderSavedData = { tenders: [] };

document.addEventListener('DOMContentLoaded', async () => {
  await loadSavedTendersFromServer();
  initializeSavedTenderCountryDropdown();
  renderSavedTenders();
  registerSavedTenderListeners();
});

async function loadSavedTendersFromServer() {
  const endpoint = `${SENSIO_API_BASE}/api/tenders/saved-tenders`;

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
    window.SensioTenderSavedData.tenders = Array.isArray(payload.tenders) ? payload.tenders : [];

    // Merge into the shared dataset (dedup by id) so openTenderModal() in
    // tenders-ui.js keeps working for saved items even if a tender has since
    // fallen out of the live scraper sweep.
    if (window.SensioTenderData) {
      await window.SensioTenderData.ready;
      const existingIds = new Set(window.SensioTenderData.tenders.map(t => t.id));
      window.SensioTenderSavedData.tenders.forEach(t => {
        if (!existingIds.has(t.id)) window.SensioTenderData.tenders.push(t);
      });
    }

    console.log(`⭐ Loaded ${window.SensioTenderSavedData.tenders.length} saved tender(s) from saved_tenders.xlsx.`);
  } catch (err) {
    console.error('[Saved Tenders]: Could not reach backend. Ensure server.py is running!', err);
    window.SensioTenderSavedData.tenders = [];
  }
}

function initializeSavedTenderCountryDropdown() {
  const select = document.getElementById('savedTenderCountrySelector');
  if (!select) return;

  const dataset = window.SensioTenderSavedData.tenders;
  const uniqueCountries = [...new Set(dataset.map(item => item.country).filter(Boolean))].sort();

  select.innerHTML = '<option value="all">🌍 All Countries</option>';
  uniqueCountries.forEach(country => {
    const opt = document.createElement('option');
    opt.value = country.toLowerCase();
    opt.textContent = country;
    select.appendChild(opt);
  });
}

function registerSavedTenderListeners() {
  const elements = ['savedTenderSearchInput', 'savedTenderCountrySelector', 'savedTenderSectorSelector'];
  elements.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', renderSavedTenders);
  });
}

// "Starred By" comes from the backend as a comma-joined string of founder names
// (derived from which per-founder saved_<name>.xlsx files hold the tender).
function parseTenderStarredBy(item) {
  return String(item.starredBy || '')
    .split(',')
    .map(s => s.trim())
    .filter(Boolean);
}

function renderSavedTenders() {
  const container = document.getElementById('savedTenderGridDisplay');
  if (!container) return;

  const searchVal  = document.getElementById('savedTenderSearchInput')?.value.toLowerCase() || '';
  const countryVal = document.getElementById('savedTenderCountrySelector')?.value || 'all';
  const sectorVal  = document.getElementById('savedTenderSectorSelector')?.value || 'all';

  // Apply the shared filters once, then split into per-founder sections below.
  let filtered = [...window.SensioTenderSavedData.tenders];

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

  // Counter = number of distinct saved tenders (a tender saved by two founders
  // still counts once here, even though it appears in both sections).
  const counter = document.getElementById('savedTenderCounterMetric');
  if (counter) counter.textContent = filtered.length;

  // Section order: the known founders first, then any other name that shows up
  // in the data (e.g. a legacy "unknown" bucket) so nothing is ever hidden.
  const known = (typeof FOUNDER_NAMES !== 'undefined' && Array.isArray(FOUNDER_NAMES)) ? FOUNDER_NAMES : [];
  const extra = [];
  filtered.forEach(item => parseTenderStarredBy(item).forEach(n => {
    if (!known.includes(n) && !extra.includes(n)) extra.push(n);
  }));
  const founders = [...known, ...extra];

  if (founders.length === 0) {
    container.innerHTML = `<div style="text-align:center; padding:48px; color:var(--text-muted);">No saved tenders yet. Bookmark items from the dashboard or Tenders tab.</div>`;
    return;
  }

  container.innerHTML = founders.map(founder => {
    const mine = filtered.filter(item => parseTenderStarredBy(item).includes(founder));
    const body = mine.length
      ? `<div class="tender-grid">${mine.map(item => tenderSavedCardHtml(item, founder)).join('')}</div>`
      : `<div style="padding:18px 4px; color:var(--text-muted); font-size:14px;">No tenders saved by ${founder} yet.</div>`;
    return `
      <section class="saved-founder-section" style="margin-bottom:34px;">
        <div style="display:flex; align-items:center; gap:10px; margin-bottom:14px; padding-bottom:8px; border-bottom:1px solid var(--border-color, #e5e7eb);">
          <span style="width:28px; height:28px; border-radius:50%; background:var(--color-teal, #0d9488); color:#fff; display:inline-flex; align-items:center; justify-content:center; font-weight:700; font-size:13px;">${founder.charAt(0).toUpperCase()}</span>
          <h2 style="font-size:16px; margin:0;">${founder}</h2>
          <span style="font-size:13px; color:var(--text-muted); font-weight:600;">${mine.length} saved</span>
        </div>
        ${body}
      </section>
    `;
  }).join('');
}

function tenderSavedCardHtml(item, founder) {
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

  // Indicate when the same tender is on more than one founder's list.
  const others = parseTenderStarredBy(item).filter(n => n !== founder);
  const sharedHtml = others.length
    ? `<div class="shared-badge" title="Also saved by ${others.join(', ')}" style="display:inline-flex; align-items:center; gap:5px; margin-top:8px; padding:3px 9px; border-radius:999px; background:#eef2ff; color:#4f46e5; font-size:12px; font-weight:600;">👥 Shared · also saved by ${others.join(', ')}</div>`
    : '';

  return `
    <div class="tender-card fade-in-up">
      <button class="save-btn saved" onclick="unsaveTenderFromSavedPage('${item.id}', '${founder}', this)" title="Remove from ${founder}'s saved">⭐</button>
      <div>
        <h3 class="tender-card-title">${item.title}</h3>
        ${badgeHtml}
        ${sharedHtml}
        <div class="tender-card-dates">
          <div class="date-row"><span class="date-label">Opening Phase:</span><span class="date-val">${item.openingDate}</span></div>
          <div class="date-row"><span class="date-label">Deadline Phase:</span><span class="date-val">${item.closingDate}</span></div>
        </div>
      </div>
      <div class="relevancy-score-container">
        <span class="relevancy-label">Relevancy Core</span>
        <span class="relevancy-pill">${(item.relevancyScore * 100).toFixed(0)}%</span>
      </div>
      <button class="btn btn-ghost" onclick="openTenderModal('${item.id}')" style="margin-top:14px; width:100%; justify-content:center; font-size:13px;">View Specifications</button>
    </div>
  `;
}

/*
  Unsave waits for the server to confirm the row was actually updated in
  saved_tenders.xlsx before touching the UI. If the request fails or gets
  cut off (e.g. by navigation), the card stays put and the button
  re-enables — so the UI can never silently drift ahead of the Excel file.
*/
function unsaveTenderFromSavedPage(tenderId, founderName, buttonEl) {
  // The star lives inside a specific founder's section, so remove it from THAT
  // founder's list — not necessarily whoever is currently signed in.
  if (!founderName) {
    alert('Could not determine which founder to unsave for.');
    return;
  }
  if (buttonEl) buttonEl.disabled = true;

  fetch(`${SENSIO_API_BASE}/api/tenders/save-tender`, {
    method: 'POST',
    mode: 'cors',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tenderId: tenderId, isSaved: false, founderName: founderName })
  })
    .then(res => {
      if (!res.ok) throw new Error(`Server returned ${res.status}`);
      return res.json();
    })
    .then(async (data) => {
      console.log('[Saved Tenders]: Removed for', founderName, '— saved count is now', data.savedCount);
      // Re-pull the merged list so a tender still saved by another founder stays
      // visible (in their section) rather than disappearing entirely.
      await loadSavedTendersFromServer();
      renderSavedTenders();
      initializeSavedTenderCountryDropdown();
    })
    .catch(err => {
      console.error('[Saved Tenders]: Failed to remove from backend.', err);
      alert('Could not remove this tender — the server did not confirm the change. Please try again.');
      if (buttonEl) buttonEl.disabled = false;
    });
}
