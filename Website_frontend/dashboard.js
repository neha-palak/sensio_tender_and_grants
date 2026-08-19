/* ═══════════════════════════════════════
   COMBINED DASHBOARD — top-5-by-relevancy preview panels for both domains.
   Reuses the save/modal logic from tenders-ui.js and grants-ui.js (already
   loaded on this page); this file only renders the two preview lists.
   ════════════════════════════════════════ */

(window.SensioTenderData ? window.SensioTenderData.ready : Promise.resolve()).then(renderTopTenders);
(window.SensioGrantData ? window.SensioGrantData.ready : Promise.resolve()).then(renderTopGrants);

function renderTopTenders() {
  const container = document.getElementById('topTendersContainer');
  const counter = document.getElementById('tendersCountMetric');
  if (!container) return;

  const records = [...(window.SensioTenderData?.tenders || [])];
  if (counter) counter.textContent = records.length;

  const top = [...records].sort((a, b) => b.relevancyScore - a.relevancyScore).slice(0, 5);
  if (top.length === 0) {
    container.innerHTML = `<div style="padding:24px; color:var(--text-muted); font-size:14px;">No tender data yet — check TENDER_DATA_DIR.</div>`;
    return;
  }

  container.innerHTML = top.map(item => {
    const daysLeft = calculateDaysRemaining(item.closingDate);
    const isSaved = isTenderSaved(item.id);
    let badgeHtml = isNaN(daysLeft)
      ? `<div class="urgency-badge" style="background-color:#f3f4f6; color:#6b7280;">No deadline</div>`
      : daysLeft < 0
        ? `<div class="urgency-badge" style="background-color:#fee2e2; color:#ef4444;">Expired</div>`
        : daysLeft <= 7
          ? `<div class="urgency-badge urgency-closing">⏰ Closing Soon (${daysLeft}d)</div>`
          : `<div class="urgency-badge urgency-active">✅ Active (${daysLeft}d)</div>`;

    return `
      <div class="tender-card fade-in-up">
        <button class="save-btn ${isSaved ? 'saved' : ''}" onclick="handleTenderSaveToggle('${item.id}', this)" title="${isSaved ? 'Remove from Saved' : 'Save Tender'}">
          ${isSaved ? '⭐' : '☆'}
        </button>
        <div>
          <h3 class="tender-card-title">${item.title}</h3>
          ${badgeHtml}
        </div>
        <div class="relevancy-score-container">
          <span class="relevancy-label">Relevancy</span>
          <span class="relevancy-pill">${(item.relevancyScore * 100).toFixed(0)}%</span>
        </div>
        <button class="btn btn-ghost" onclick="openTenderModal('${item.id}')" style="margin-top:14px; width:100%; justify-content:center; font-size:13px;">View Specifications</button>
      </div>
    `;
  }).join('');
}

function renderTopGrants() {
  const container = document.getElementById('topGrantsContainer');
  const counter = document.getElementById('grantsCountMetric');
  if (!container) return;

  const records = [...(window.SensioGrantData?.grants || [])];
  if (counter) counter.textContent = records.length;

  const top = [...records].sort((a, b) => b.relevancyScore - a.relevancyScore).slice(0, 5);
  if (top.length === 0) {
    container.innerHTML = `<div style="padding:24px; color:var(--text-muted); font-size:14px;">No grant data yet — check GRANT_DATA_DIR.</div>`;
    return;
  }

  container.innerHTML = top.map(item => {
    const daysLeft = calculateDaysRemaining(item.closingDate);
    const isSaved = isGrantSaved(item.id);
    let badgeHtml = isNaN(daysLeft)
      ? `<div class="urgency-badge" style="background-color:#f3f4f6; color:#6b7280;">No deadline</div>`
      : daysLeft < 0
        ? `<div class="urgency-badge" style="background-color:#fee2e2; color:#ef4444;">Expired</div>`
        : daysLeft <= 7
          ? `<div class="urgency-badge urgency-closing">⏰ Closing Soon (${daysLeft}d)</div>`
          : `<div class="urgency-badge urgency-active">✅ Active (${daysLeft}d)</div>`;

    return `
      <div class="grant-card fade-in-up">
        <button class="save-btn ${isSaved ? 'saved' : ''}" onclick="handleGrantSaveToggle('${item.id}', this)" title="${isSaved ? 'Remove from Saved' : 'Save Grant'}">
          ${isSaved ? '⭐' : '☆'}
        </button>
        <div>
          <h3 class="grant-card-title">${item.title}</h3>
          ${badgeHtml}
        </div>
        <div class="relevancy-score-container">
          <span class="relevancy-label">Relevancy</span>
          <span class="relevancy-pill">${(item.relevancyScore * 100).toFixed(0)}%</span>
        </div>
        <button class="btn btn-ghost" onclick="openGrantModal('${item.id}')" style="margin-top:14px; width:100%; justify-content:center; font-size:13px;">View Specifications</button>
      </div>
    `;
  }).join('');
}
