/* ═══════════════════════════════════════
   LIVE SPREADSHEET STORAGE ENGINE LINK — Tenders
   ════════════════════════════════════════ */
let resolveTenderDataReady;
window.SensioTenderData = {
  sourceFile: "Sensio Excel Data Hub",
  tenders: [],
  ready: new Promise(resolve => { resolveTenderDataReady = resolve; })
};

// Single source of truth for "is this tender starred BY ME" — pulled from the
// backend scoped to the current founder, never from localStorage. A star is only
// filled for tenders the signed-in founder saved, not ones teammates saved.
window.SensioTenderSavedIds = new Set();

// (Re)load the current founder's own saved ids. Call again after the founder
// switches so the stars update to that person's saves.
async function reloadTenderSavedIdsForCurrentFounder() {
  const founder = localStorage.getItem('sensio_founder_identity') || '';
  try {
    const idsRes = await fetch(
      `${SENSIO_API_BASE}/api/tenders/saved-ids?founder=${encodeURIComponent(founder)}`,
      { method: 'GET', mode: 'cors', headers: { 'Accept': 'application/json' } }
    );
    if (idsRes.ok) {
      const idsPayload = await idsRes.json();
      window.SensioTenderSavedIds = new Set((idsPayload.savedIds || []).map(String));
    }
  } catch (err) {
    console.error('[Saved IDs]: Could not load saved-ids from backend.', err);
  }
}

(async function initializeSensioTenderStream() {
  const endpoint = `${SENSIO_API_BASE}/api/tenders/sensio-stream`;

  try {
    const response = await fetch(endpoint, {
      method: 'GET', mode: 'cors', headers: { 'Accept': 'application/json' }
    });
    if (!response.ok) throw new Error(`Server returned invalid status code: ${response.status}`);
    const streamPayload = await response.json();
    window.SensioTenderData.tenders = Array.isArray(streamPayload.tenders) ? streamPayload.tenders : [];
    window.SensioTenderData.sourceFile = streamPayload.sourceFile || window.SensioTenderData.sourceFile;
    console.log(`⚡ Tender sheet connected successfully! Loaded ${window.SensioTenderData.tenders.length} active rows into UI.`);
  } catch (err) {
    console.error('Dashboard failed to read from Tender API. Ensure server.py is running! Error: ', err);
    window.SensioTenderData.tenders = [];
  }

  // Load the current founder's own saved-ids from the backend (per-user star
  // state). If no founder is chosen yet, this returns empty and the identity
  // modal will trigger a reload once they pick who they are.
  await reloadTenderSavedIdsForCurrentFounder();

  if (typeof resolveTenderDataReady === 'function') {
    resolveTenderDataReady(window.SensioTenderData);
  }
})();
