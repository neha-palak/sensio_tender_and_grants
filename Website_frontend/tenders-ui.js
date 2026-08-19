/* ═══════════════════════════════════════
   TENDER-SPECIFIC save/modal logic. Shared chrome (sidebar, founder identity,
   quit, generic modal dismissal) lives in shared-ui.js.
   ════════════════════════════════════════ */

/* SAVE STATE — sourced entirely from window.SensioTenderSavedIds (loaded from
   saved_tenders.xlsx via the backend in tenders-data.js). No localStorage. */
function isTenderSaved(id) {
  return window.SensioTenderSavedIds ? window.SensioTenderSavedIds.has(String(id)) : false;
}

function handleTenderSaveToggle(tenderId, element) {
  const founderName = getCurrentFounder();
  if (!founderName) {
    ensureFounderIdentity().then(() => handleTenderSaveToggle(tenderId, element));
    return;
  }

  const nextSaved = !isTenderSaved(tenderId);
  element.disabled = true;

  fetch(`${SENSIO_API_BASE}/api/tenders/save-tender`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tenderId: tenderId, isSaved: nextSaved, founderName: founderName })
  })
    .then(response => {
      if (!response.ok) throw new Error(`Server returned ${response.status}`);
      return response.json();
    })
    .then(data => {
      if (nextSaved) window.SensioTenderSavedIds.add(String(tenderId));
      else window.SensioTenderSavedIds.delete(String(tenderId));
      element.classList.toggle('saved', nextSaved);
      element.innerHTML = nextSaved ? '⭐' : '☆';
      element.title = nextSaved ? 'Remove from Saved' : 'Save Tender';
      if (typeof executePipelineQueryRender === 'function') executePipelineQueryRender();
      if (typeof renderSavedTenders === 'function') renderSavedTenders();
    })
    .catch(err => {
      console.error('[Sync Server App Failure]:', err);
      alert('Could not update the saved list — the server did not confirm the change. Please try again.');
    })
    .finally(() => { element.disabled = false; });
}

function openTenderModal(tenderId) {
  const tenders = window.SensioTenderData?.tenders || [];
  const tender = tenders.find(t => t.id === tenderId);
  if (!tender) return;

  const overlay = document.getElementById('tenderModalOverlay');
  if (!overlay) return;

  overlay.querySelector('#modalTitle').textContent = tender.title;
  overlay.querySelector('#modalDescription').textContent = tender.description;
  overlay.querySelector('#modalEligibility').textContent = tender.eligibility;

  const grid = overlay.querySelector('#modalGrid');
  if (grid) {
    grid.innerHTML = `
      <div class="modal-section"><div class="modal-section-label">Country</div><div><strong>${tender.country}</strong></div></div>
      <div class="modal-section"><div class="modal-section-label">Category</div><div style="text-transform: capitalize;">${tender.category}</div></div>
      <div class="modal-section"><div class="modal-section-label">Budget Range</div><div>₹${tender.budgetINR.toLocaleString('en-IN')}</div></div>
      <div class="modal-section"><div class="modal-section-label">Relevancy Weight</div><div><strong>${(tender.relevancyScore * 100).toFixed(0)}% Match</strong></div></div>
    `;
  }

  const linkWrap = overlay.querySelector('#modalLink');
  if (linkWrap) {
    linkWrap.innerHTML = `<a href="${tender.link}" target="_blank" class="btn btn-primary" style="margin-top:6px; font-size:13px;">🌐 View Raw Source File Portal</a>`;
  }

  overlay.style.display = 'flex';
}
