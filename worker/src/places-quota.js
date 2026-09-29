// Counts widget launches through this app; Google API-key restrictions are separate.
const ZONE = 'America/Los_Angeles';
function parts(ms) {
  return Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
    timeZone: ZONE, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date(ms)).filter(p => p.type !== 'literal').map(p => [p.type, p.value]));
}
function midnight(year, month, day) {
  const target = Date.UTC(year, month - 1, day);
  let guess = target;
  for (let i = 0; i < 4; i++) {
    const p = parts(guess);
    guess += target - Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
  }
  return new Date(guess).toISOString();
}
export function quotaWindow(now = Date.now()) {
  const p = parts(now);
  return {day: `${p.year}-${p.month}-${p.day}`, month: `${p.year}-${p.month}`,
    resetAt: midnight(+p.year, +p.month, +p.day + 1),
    monthlyResetAt: midnight(+p.year, +p.month + 1, 1), timeZone: ZONE};
}
function limit(value, fallback, max) {
  const n = value === undefined || value === '' ? fallback : Number(value);
  if (!Number.isSafeInteger(n) || n < 0 || n > max) throw new Error('Invalid Places quota configuration');
  return n;
}
export function quotaLimits(env) {
  return {daily: limit(env.PLACES_DAILY_LIMIT, 20, 1000),
    monthly: limit(env.PLACES_MONTHLY_LIMIT, 9000, 10000)};
}
export function validPermitRequest(body) {
  return body && typeof body === 'object' && !Array.isArray(body)
    && typeof body.placeId === 'string' && /^[A-Za-z0-9_-]{5,256}$/.test(body.placeId)
    && typeof body.requestId === 'string' && /^[A-Za-z0-9_-]{16,80}$/.test(body.requestId);
}
// A monthly object keeps both limits in one transaction across users and tabs.
export class PlacesQuota {
  constructor(ctx, env) { this.storage = ctx.storage; this.env = env; }
  async fetch(req) {
    const body = await req.json();
    if (!validPermitRequest(body) || !/^[a-f0-9]{64}$/.test(body.user || ''))
      return Response.json({error: 'invalid_request'}, {status: 400});
    const window = quotaWindow();
    if (body.month !== window.month) return Response.json({error: 'unavailable'}, {status: 503});
    const limits = quotaLimits(this.env);
    const result = await this.storage.transaction(async tx => {
      const grantKey = `grant:${body.user}:${body.requestId}`;
      const previous = await tx.get(grantKey);
      if (previous) {
        if (previous.placeId !== body.placeId || previous.day !== window.day)
          return {status: 409, body: {error: 'request_conflict'}};
        return {status: 200, body: previous.reply};
      }
      const userKey = `daily:${window.day}:${body.user}`;
      const daily = (await tx.get(userKey)) || 0;
      const monthly = (await tx.get('monthly')) || 0;
      const common = {resetAt: window.resetAt, monthlyResetAt: window.monthlyResetAt, timeZone: window.timeZone};
      if (monthly >= limits.monthly)
        return {status: 429, body: {...common, error: 'monthly_limit', resetAt: window.monthlyResetAt}};
      if (daily >= limits.daily) return {status: 429, body: {...common, error: 'daily_limit'}};
      const reply = {...common, allowed: true, requestId: body.requestId, remainingDaily: limits.daily - daily - 1};
      await tx.put(userKey, daily + 1);
      await tx.put('monthly', monthly + 1);
      await tx.put(grantKey, {day: window.day, placeId: body.placeId, reply});
      return {status: 200, body: reply};
    });
    if (result.status === 200 && !(await this.storage.getAlarm()))
      await this.storage.setAlarm(Date.parse(window.monthlyResetAt) + 7 * 86400000);
    return Response.json(result.body, {status: result.status});
  }
  async alarm() { await this.storage.deleteAll(); }
}
