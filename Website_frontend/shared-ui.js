/* ═══════════════════════════════════════
   SHARED CHROME — sidebar, modals, founder identity, quit.
   Domain-specific save/modal logic lives in tenders-ui.js / grants-ui.js.
   ════════════════════════════════════════ */

// The API lives on the SAME Flask app that serves these pages, so relative URLs
// follow whatever port it was opened on. The fallback only matters if someone
// opens the .html straight off disk (file://), where there's no server origin.
const SENSIO_API_BASE =
  (location.protocol === 'http:' || location.protocol === 'https:')
    ? ''
    : 'http://127.0.0.1:5001';

document.addEventListener('DOMContentLoaded', () => {
  setupSidebarControls();
  setupModalDismissals();
  ensureFounderIdentity().then(updateIdentityBadge);
});

function setupSidebarControls() {
  const toggleBtn = document.getElementById('sidebarToggleBtn');
  const sidebar = document.getElementById('sidebar');
  const mainContent = document.getElementById('mainContent');

  if (toggleBtn && sidebar && mainContent) {
    toggleBtn.addEventListener('click', () => {
      sidebar.classList.toggle('collapsed');
      mainContent.classList.toggle('collapsed');
      toggleBtn.querySelector('span').textContent = sidebar.classList.contains('collapsed') ? '▶' : '◀';
    });
  }
}

// Generic across every modal overlay on the page (tenderModalOverlay,
// grantModalOverlay, ...) so a page that loads both domains needs no
// per-domain dismissal wiring.
function setupModalDismissals() {
  document.querySelectorAll('[data-modal-close]').forEach(btn => {
    btn.addEventListener('click', () => {
      const targetEl = document.getElementById(btn.getAttribute('data-modal-close'));
      if (targetEl) targetEl.style.display = 'none';
    });
  });

  document.querySelectorAll('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) overlay.style.display = 'none';
    });
  });
}

function calculateDaysRemaining(targetDateStr) {
  if (!targetDateStr) return NaN;
  const s = String(targetDateStr).trim();
  if (!s || s.toLowerCase() === 'n/a') return NaN;

  // Parse "YYYY-MM-DD" from its parts to avoid the UTC-vs-local off-by-one that
  // `new Date("2026-08-01")` (parsed as UTC midnight) causes in most timezones.
  let target;
  const iso = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (iso) {
    target = new Date(Number(iso[1]), Number(iso[2]) - 1, Number(iso[3]));
  } else {
    target = new Date(s);
  }
  if (isNaN(target.getTime())) return NaN;

  // Compare against TODAY (not a hardcoded date), at day granularity so an item
  // closing today reads as 0 and tomorrow as 1.
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfTarget = new Date(target.getFullYear(), target.getMonth(), target.getDate());
  return Math.round((startOfTarget - startOfToday) / (1000 * 60 * 60 * 24));
}

// 👈 replace with your actual founder names
const FOUNDER_NAMES = ["Venkatesh", "Kenneth", "Mohan"];

function getCurrentFounder() {
  return localStorage.getItem('sensio_founder_identity') || null;
}

function setCurrentFounder(name) {
  localStorage.setItem('sensio_founder_identity', name);
}

function ensureFounderIdentity() {
  return new Promise(resolve => {
    const existing = getCurrentFounder();
    if (existing) { resolve(existing); return; }
    showIdentityModal(resolve);
  });
}

function showIdentityModal(onSelect) {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.style.display = 'flex';
  overlay.innerHTML = `
    <div class="modal" style="max-width:360px;">
      <div class="modal-header"><div class="modal-title">Who's using Sensio?</div></div>
      <div class="modal-section">
        <select id="founderIdentitySelect" class="filter-select" style="width:100%;">
          ${FOUNDER_NAMES.map(n => `<option value="${n}">${n}</option>`).join('')}
        </select>
      </div>
      <div class="modal-footer">
        <button class="btn btn-primary" id="founderIdentityConfirmBtn" style="width:100%; justify-content:center;">Continue</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  document.getElementById('founderIdentityConfirmBtn').addEventListener('click', async () => {
    const name = document.getElementById('founderIdentitySelect').value;
    setCurrentFounder(name);
    document.body.removeChild(overlay);
    updateIdentityBadge();
    // Reload star state for the newly-chosen founder and re-render so stars
    // reflect THIS person's saves (not whoever was signed in before).
    await refreshSavedStateForCurrentFounder();
    onSelect(name);
  });
}

// Re-pull the current founder's saved ids for whichever domain(s) are loaded on
// this page, then re-render. Each call is typeof-guarded so this one function
// works unmodified whether the page has tenders, grants, or both wired up.
async function refreshSavedStateForCurrentFounder() {
  if (typeof reloadTenderSavedIdsForCurrentFounder === 'function') await reloadTenderSavedIdsForCurrentFounder();
  if (typeof reloadGrantSavedIdsForCurrentFounder === 'function') await reloadGrantSavedIdsForCurrentFounder();
  if (typeof executePipelineQueryRender === 'function') executePipelineQueryRender();
  if (typeof renderSavedTenders === 'function') renderSavedTenders();
  if (typeof renderSavedGrants === 'function') renderSavedGrants();
}

function quitApp() {
  const ok = confirm(
    "Quit Sensio Dashboard?\n\n" +
    "This stops the app on YOUR computer only — your teammates are not affected, " +
    "and everything you've saved is already stored in the shared folder."
  );
  if (!ok) return;
  // Tell the local server to shut itself down, then show a friendly closed screen.
  // The request often never gets a response (the server exits mid-reply), so we
  // update the page in .finally() regardless.
  fetch('/api/shutdown', { method: 'POST' }).catch(() => {}).finally(() => {
    document.body.innerHTML =
      '<div style="min-height:100vh; display:flex; align-items:center; justify-content:center; ' +
      'font-family: system-ui, -apple-system, sans-serif; background:#f9fafb; color:#374151; ' +
      'text-align:center; padding:40px;">' +
      '<div><div style="font-size:44px; margin-bottom:16px;">👋</div>' +
      '<h1 style="font-size:22px; margin:0 0 8px;">Sensio Dashboard has shut down</h1>' +
      '<p style="color:#6b7280; margin:0;">You can safely close this browser tab now.</p></div></div>';
  });
}

function updateIdentityBadge() {
  const avatar = document.querySelector('.avatar');
  const name = getCurrentFounder();
  if (avatar && name) {
    avatar.textContent = name.charAt(0).toUpperCase();
    avatar.title = `Signed in as ${name} — click to switch`;
    avatar.style.cursor = 'pointer';
    avatar.onclick = () => showIdentityModal(() => updateIdentityBadge());
  }
}
