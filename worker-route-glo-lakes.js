// ============================================================================
// JalPrahari worker route: GET /glo-lakes
// Serves the near-real-time lake-area feed produced by the GLO pipeline
// (Google Earth Engine -> GitHub Actions -> data/glo-lakes-latest.json).
// Paste this into worker.js next to the /news route, set GLO_LAKES_JSON_URL
// to your repo's raw URL, and redeploy the worker.
// ============================================================================

// >>> EDIT THIS: raw URL of the JSON your Actions workflow commits <<<
const GLO_LAKES_JSON_URL =
  'https://raw.githubusercontent.com/<GITHUB_USER>/<REPO>/main/data/glo-lakes-latest.json';

const GLO_LAKES_CACHE_TTL_MS = 60 * 60 * 1000; // 1 hour

function gloLakesCacheKey(request) {
  return new Request(new URL('/__jalprahari_glo_lakes_v1', request.url).toString(), {
    headers: { 'Accept': 'application/json' },
  });
}

async function handleGloLakes(request) {
  const cors = {
    'Access-Control-Allow-Origin': '*',
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'public, max-age=600',
  };
  const empty = (why) =>
    new Response(
      JSON.stringify({ updated_at: new Date().toISOString(), stale: true,
                       lakes: [], note: why }),
      { status: 200, headers: cors });

  // Serve cache while fresh.
  try {
    const cached = await caches.default.match(gloLakesCacheKey(request));
    if (cached) {
      const body = await cached.json();
      const age = Date.now() - Date.parse(body.updated_at || 0);
      if (Number.isFinite(age) && age >= 0 && age < GLO_LAKES_CACHE_TTL_MS &&
          Array.isArray(body.lakes) && body.lakes.length > 0) {
        return new Response(JSON.stringify({ ...body, stale: false }),
                            { status: 200, headers: cors });
      }
    }
  } catch (_) { /* fall through to upstream */ }

  // Fetch the pipeline output from GitHub.
  try {
    const res = await fetch(GLO_LAKES_JSON_URL, {
      headers: { 'Accept': 'application/json', 'User-Agent': 'jalprahari-glo/1.0' },
      signal: AbortSignal.timeout(12000),
    });
    if (!res.ok) throw new Error('glo-lakes upstream HTTP ' + res.status);
    const payload = await res.json();
    if (!payload || !Array.isArray(payload.lakes)) throw new Error('bad payload');
    const out = { ...payload, stale: false };
    try {
      await caches.default.put(
        gloLakesCacheKey(request),
        new Response(JSON.stringify(out), { headers: { 'Content-Type': 'application/json' } }));
    } catch (_) {}
    return new Response(JSON.stringify(out), { status: 200, headers: cors });
  } catch (e) {
    // Stale cache beats nothing; honest empty beats invented data.
    try {
      const cached = await caches.default.match(gloLakesCacheKey(request));
      if (cached) {
        const body = await cached.json();
        return new Response(JSON.stringify({ ...body, stale: true }),
                            { status: 200, headers: cors });
      }
    } catch (_) {}
    return empty('lake-area pipeline unavailable: ' + String(e && e.message || e));
  }
}

// In the worker's fetch handler, add:
//   if (url.pathname === '/glo-lakes') return handleGloLakes(request);
// And list it in the /health route map:
//   '/glo-lakes': 'near-real-time GLOF lake areas from the GEE pipeline (cached 1h, CORS *)',
