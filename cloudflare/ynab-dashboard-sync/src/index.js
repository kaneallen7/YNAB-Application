const GITHUB_API = 'https://api.github.com';

async function dispatchSync(env, trigger) {
  if (!env.GITHUB_TOKEN) {
    throw new Error('GITHUB_TOKEN secret is not configured');
  }

  const response = await fetch(`${GITHUB_API}/repos/${env.GITHUB_REPOSITORY}/dispatches`, {
    method: 'POST',
    headers: {
      Accept: 'application/vnd.github+json',
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      'Content-Type': 'application/json',
      'User-Agent': 'ynab-dashboard-sync-worker',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    body: JSON.stringify({
      event_type: 'ynab-sync',
      client_payload: { source: 'cloudflare-cron', trigger },
    }),
  });

  if (!response.ok) {
    console.error(JSON.stringify({ event: 'github_dispatch_failed', status: response.status }));
    throw new Error(`GitHub dispatch failed with HTTP ${response.status}`);
  }

  console.log(JSON.stringify({ event: 'ynab_sync_dispatched', trigger }));
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(dispatchSync(env, controller.cron));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/health') {
      return Response.json({ ok: true, service: 'ynab-dashboard-sync' }, {
        headers: { 'Cache-Control': 'no-store' },
      });
    }
    return new Response('YNAB dashboard sync scheduler is running.\n', {
      headers: { 'Cache-Control': 'no-store' },
    });
  },
};
