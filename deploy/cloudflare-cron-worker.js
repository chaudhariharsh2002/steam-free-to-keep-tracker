/**
 * Cloudflare Worker Cron Trigger Dispatcher (Free Tier: up to 5 Cron Triggers).
 * Reliably triggers the hosted Steam Free-to-Keep Tracker backend every 60 minutes,
 * completely independent of user devices or browsers.
 *
 * Configure secrets via Wrangler (NEVER hardcode secrets):
 *   wrangler secret put TRACKER_API_URL
 *   wrangler secret put TRIGGER_API_KEY
 */
export default {
  async scheduled(event, env, ctx) {
    const baseUrl = (env.TRACKER_API_URL || "").replace(/\/+$/, "");
    if (!baseUrl) {
      console.error("TRACKER_API_URL secret is not configured.");
      return;
    }

    const endpoint = `${baseUrl}/api/v1/scans/trigger`;
    const headers = { "Content-Type": "application/json" };
    if (env.TRIGGER_API_KEY) {
      headers["Authorization"] = `Bearer ${env.TRIGGER_API_KEY}`;
    }

    const resp = await fetch(endpoint, { method: "POST", headers });
    const body = await resp.text();
    console.log(
      JSON.stringify({
        scheduledTime: new Date(event.scheduledTime).toISOString(),
        status: resp.status,
        body: body.slice(0, 500),
      })
    );
    if (!resp.ok && resp.status !== 409) {
      throw new Error(`Trigger failed with HTTP ${resp.status}: ${body}`);
    }
  },
};
