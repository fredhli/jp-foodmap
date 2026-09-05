// Map-backed Workers KV mock with (optional) eventual consistency.
// Lifted from audit_outputs/fable-2026-09-05/04-sync-and-worker/harness/ and
// extended with delete() for DELETE /api/state (M-008).
//
// Each binding is tied to a "PoP" name. A put made at PoP X is visible at X
// immediately; other PoPs only see it once propagationMs has elapsed —
// mirrors Cloudflare's documented behaviour ("usually immediately visible at
// the location where made … up to 60 seconds or more elsewhere").
// latencyMs widens the get→put gap inside the Worker so two calls at the
// SAME PoP can interleave (the intra-PoP race).
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class KVMock {
  constructor(opts = {}) {
    this.propagationMs = opts.propagationMs || 0;
    this.latencyMs = opts.latencyMs || 0;
    this.writes = new Map();       // key -> [{pop, t, value}] in commit order
    this.puts = 0; this.gets = 0; this.deletes = 0;
    this.putHook = null;           // async (key, value) => void; may throw to simulate 429/outage
    this.getHook = null;
    this.deleteHook = null;
    this.lastPutAt = new Map();    // key -> ms, for the 1 write/s/key rule
    this.enforceRate = opts.enforceRate || false;
  }
  binding(pop = 'FRA') {
    const self = this;
    return {
      async get(key) {
        self.gets++;
        if (self.getHook) await self.getHook(key);
        if (self.latencyMs) await sleep(self.latencyMs);
        const now = Date.now();
        const list = self.writes.get(key) || [];
        let best = null;
        for (const w of list) {
          if (w.pop === pop || w.t + self.propagationMs <= now) best = w;
        }
        return best && best.value !== null ? best.value : null;
      },
      async put(key, value) {
        self.puts++;
        if (self.putHook) await self.putHook(key, value);
        if (self.latencyMs) await sleep(self.latencyMs);
        if (self.enforceRate) {
          const last = self.lastPutAt.get(key) || 0;
          if (Date.now() - last < 1000) {
            throw new Error('KV PUT failed: 429 Too Many Requests (1 write/second/key)');
          }
          self.lastPutAt.set(key, Date.now());
        }
        const list = self.writes.get(key) || [];
        list.push({pop, t: Date.now(), value});
        self.writes.set(key, list);
      },
      async delete(key) {
        self.deletes++;
        if (self.deleteHook) await self.deleteHook(key);
        if (self.latencyMs) await sleep(self.latencyMs);
        const list = self.writes.get(key) || [];
        list.push({pop, t: Date.now(), value: null});
        self.writes.set(key, list);
      },
    };
  }
  // Latest committed value regardless of PoP visibility (null once deleted).
  primary(key) {
    const l = this.writes.get(key) || [];
    return l.length ? l[l.length - 1].value : null;
  }
  seed(key, value, pop = 'FRA', ageMs = 0) {
    this.writes.set(key, [{pop, t: Date.now() - ageMs, value}]);
  }
  history(key) { return (this.writes.get(key) || []).map((w) => ({pop: w.pop, v: safeV(w.value)})); }
}
function safeV(s) { try { return JSON.parse(s).v; } catch (_) { return null; } }
