/* ═══════════════════════════════════════
   GRANT-SPECIFIC save/modal logic. Shared chrome (sidebar, founder identity,
   quit, generic modal dismissal) lives in shared-ui.js.
   ════════════════════════════════════════ */

/* SAVE STATE — sourced entirely from window.SensioGrantSavedIds (loaded from
   saved_grants.xlsx via the backend in grants-data.js). No localStorage. */
function isGrantSaved(id) {
  return window.SensioGrantSavedIds ? window.SensioGrantSavedIds.has(String(id)) : false;
}

function handleGrantSaveToggle(grantId, element) {
  const founderName = getCurrentFounder();
  if (!founderName) {
    ensureFounderIdentity().then(() => handleGrantSaveToggle(grantId, element));
    return;
  }

  const nextSaved = !isGrantSaved(grantId);
  element.disabled = true;

  fetch(`${SENSIO_API_BASE}/api/grants/save-grant`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ grantId: grantId, isSaved: nextSaved, founderName: founderName })
  })
    .then(response => {
      if (!response.ok) throw new Error(`Server returned ${response.status}`);
      return response.json();
    })
    .then(data => {
      if (nextSaved) window.SensioGrantSavedIds.add(String(grantId));
      else window.SensioGrantSavedIds.delete(String(grantId));
      element.classList.toggle('saved', nextSaved);
      element.innerHTML = nextSaved ? '⭐' : '☆';
      element.title = nextSaved ? 'Remove from Saved' : 'Save Grant';
      if (typeof executePipelineQueryRender === 'function') executePipelineQueryRender();
      if (typeof renderSavedGrants === 'function') renderSavedGrants();
    })
    .catch(err => {
      console.error('[Sync Server App Failure]:', err);
      alert('Could not update the saved list — the server did not confirm the change. Please try again.');
    })
    .finally(() => { element.disabled = false; });
}

function openGrantModal(grantId) {
  const grants = window.SensioGrantData?.grants || [];
  const grant = grants.find(t => t.id === grantId);
  if (!grant) return;

  const overlay = document.getElementById('grantModalOverlay');
  if (!overlay) return;

  overlay.querySelector('#modalTitle').textContent = grant.title;
  overlay.querySelector('#modalDescription').textContent = grant.description;
  overlay.querySelector('#modalEligibility').textContent = grant.eligibility;

  const grid = overlay.querySelector('#modalGrid');
  if (grid) {
    grid.innerHTML = `
      <div class="modal-section"><div class="modal-section-label">Country</div><div><strong>${grant.country}</strong></div></div>
      <div class="modal-section"><div class="modal-section-label">Category</div><div style="text-transform: capitalize;">${grant.category}</div></div>
      <div class="modal-section"><div class="modal-section-label">Budget Range</div><div>₹${grant.budgetINR.toLocaleString('en-IN')}</div></div>
      <div class="modal-section"><div class="modal-section-label">Relevancy Weight</div><div><strong>${(grant.relevancyScore * 100).toFixed(0)}% Match</strong></div></div>
    `;
  }

  const linkWrap = overlay.querySelector('#modalLink');
  if (linkWrap) {
    linkWrap.innerHTML = `<a href="${grant.link}" target="_blank" class="btn btn-primary" style="margin-top:6px; font-size:13px;">🌐 View Raw Source File Portal</a>`;
  }

  overlay.style.display = 'flex';
}
