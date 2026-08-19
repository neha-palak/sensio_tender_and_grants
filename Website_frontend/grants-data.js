/* ═══════════════════════════════════════
   LIVE SPREADSHEET STORAGE ENGINE LINK — Grants
   ════════════════════════════════════════ */
let resolveGrantDataReady;
window.SensioGrantData = {
  sourceFile: "Sensio Excel Data Hub",
  grants: [],
  ready: new Promise(resolve => { resolveGrantDataReady = resolve; })
};

// Single source of truth for "is this grant starred BY ME" — pulled from the
// backend scoped to the current founder, never from localStorage. A star is only
// filled for grants the signed-in founder saved, not ones teammates saved.
window.SensioGrantSavedIds = new Set();

// (Re)load the current founder's own saved ids. Call again after the founder
// switches so the stars update to that person's saves.
async function reloadGrantSavedIdsForCurrentFounder() {
  const founder = localStorage.getItem('sensio_founder_identity') || '';
  try {
    const idsRes = await fetch(
      `${SENSIO_API_BASE}/api/grants/saved-ids?founder=${encodeURIComponent(founder)}`,
      { method: 'GET', mode: 'cors', headers: { 'Accept': 'application/json' } }
    );
    if (idsRes.ok) {
      const idsPayload = await idsRes.json();
      window.SensioGrantSavedIds = new Set((idsPayload.savedIds || []).map(String));
    }
  } catch (err) {
    console.error('[Saved IDs]: Could not load saved-ids from backend.', err);
  }
}

(async function initializeSensioGrantStream() {
  const endpoint = `${SENSIO_API_BASE}/api/grants/sensio-stream`;

  try {
    const response = await fetch(endpoint, {
      method: 'GET', mode: 'cors', headers: { 'Accept': 'application/json' }
    });
    if (!response.ok) throw new Error(`Server returned invalid status code: ${response.status}`);
    const streamPayload = await response.json();
    window.SensioGrantData.grants = Array.isArray(streamPayload.grants) ? streamPayload.grants : [];
    window.SensioGrantData.sourceFile = streamPayload.sourceFile || window.SensioGrantData.sourceFile;
    console.log(`⚡ Grant sheet connected successfully! Loaded ${window.SensioGrantData.grants.length} active rows into UI.`);
  } catch (err) {
    console.error('Dashboard failed to read from Grant API. Ensure server.py is running! Error: ', err);
    window.SensioGrantData.grants = [];
  }

  // Load the current founder's own saved-ids from the backend (per-user star
  // state). If no founder is chosen yet, this returns empty and the identity
  // modal will trigger a reload once they pick who they are.
  await reloadGrantSavedIdsForCurrentFounder();

  if (typeof resolveGrantDataReady === 'function') {
    resolveGrantDataReady(window.SensioGrantData);
  }
})();
