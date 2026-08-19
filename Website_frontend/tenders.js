/* ═══════════════════════════════════════
   PIPELINE FILTER ENGINE IMPLEMENTATION
   ════════════════════════════════════════ */

const loadTenderUI = () => {
  initializeFilterDropdowns();
  executePipelineQueryRender();
  registerFilterInputListeners();
};

const onTenderPageReady = () => {
  const dataReady = window.SensioTenderData?.ready ?? Promise.resolve();
  dataReady.then(loadTenderUI).catch(() => loadTenderUI());
};

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', onTenderPageReady);
} else {
  onTenderPageReady();
}

function initializeFilterDropdowns() {
  const select = document.getElementById('countrySelector');
  if (!select) return;

  const dataset = window.SensioTenderData?.tenders || [];
  // Dynamically extract unique countries from data records
  const uniqueCountries = [...new Set(dataset.map(item => item.country))].sort();

  uniqueCountries.forEach(country => {
    const opt = document.createElement('option');
    opt.value = country.toLowerCase();
    opt.textContent = country;
    select.appendChild(opt);
  });
}

function registerFilterInputListeners() {
  const elements = [
    'liveSearchInput',
    'countrySelector',
    'statusSelector',
    'sectorSelector',
    'sortEngineSelector'
  ];

  elements.forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener('input', executePipelineQueryRender);
    }
  });
}

function executePipelineQueryRender() {
  const grid = document.getElementById('pipelineGridDisplay');
  if (!grid) return;

  const searchVal  = document.getElementById('liveSearchInput')?.value.toLowerCase() || '';
  const countryVal = document.getElementById('countrySelector')?.value || 'all';
  const statusVal  = document.getElementById('statusSelector')?.value || 'all';
  const sectorVal  = document.getElementById('sectorSelector')?.value || 'all';
  const sortVal    = document.getElementById('sortEngineSelector')?.value || 'relevancy-desc';

  let filtered = [...(window.SensioTenderData?.tenders || [])];

  // 1. Text Search Filter Loop
  if (searchVal.trim() !== '') {
    filtered = filtered.filter(item => 
      item.title.toLowerCase().includes(searchVal) || 
      item.description.toLowerCase().includes(searchVal)
    );
  }

  // 2. Country Parameter Selection Matcher
  if (countryVal !== 'all') {
    filtered = filtered.filter(item => item.country.toLowerCase() === countryVal);
  }

  // 3. Category Sector Filter
  if (sectorVal !== 'all') {
    filtered = filtered.filter(item => item.category.toLowerCase() === sectorVal);
  }

  // 4. Time Interval Windows Evaluator
  if (statusVal !== 'all') {
    filtered = filtered.filter(item => {
      const remaining = calculateDaysRemaining(item.closingDate);
      if (statusVal === 'closing') {
        return remaining >= 0 && remaining <= 7;
      } else if (statusVal === 'active') {
        return remaining >= 0;
      } else if (statusVal === 'opening') {
        const openDiff = calculateDaysRemaining(item.openingDate);
        return openDiff <= 0 && openDiff >= -7;
      }
      return true;
    });
  }

  // 5. Multi-Mode Sorting Framework Evaluators
  if (sortVal === 'relevancy-desc') {
    filtered.sort((a, b) => b.relevancyScore - a.relevancyScore);
  } else if (sortVal === 'expiry-asc') {
    filtered.sort((a, b) => new Date(a.closingDate) - new Date(b.closingDate));
  } else if (sortVal === 'budget-desc') {
    filtered.sort((a, b) => b.budgetINR - a.budgetINR);
  }

  // Set the total matches counter metric element
  const counter = document.getElementById('counterMetric');
  if (counter) counter.textContent = filtered.length;

  if (filtered.length === 0) {
    grid.innerHTML = `<div style="grid-column: 1/-1; text-align: center; padding: 48px; color: var(--text-muted);">No matching pipeline tender records identified for chosen criteria.</div>`;
    return;
  }

  // Render out structured tender overview cards
  grid.innerHTML = filtered.map(item => {
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

    const isSaved = isTenderSaved(item.id);

    return `
      <div class="tender-card fade-in-up">
        <button class="save-btn ${isSaved ? 'saved' : ''}" onclick="handleTenderSaveToggle('${item.id}', this)" title="${isSaved ? 'Remove from Saved' : 'Save Tender'}">
          ${isSaved ? '⭐' : '☆'}
        </button>
        <div>
          <h3 class="tender-card-title">${item.title}</h3>
          ${badgeHtml}
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
  }).join('');
}