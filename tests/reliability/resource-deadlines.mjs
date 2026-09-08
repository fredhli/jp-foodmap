import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

const baseline = process.argv.includes('--baseline');
const read = p => baseline ? execFileSync('git', ['show', '462f807:' + p], {encoding:'utf8', maxBuffer:10e6}) : fs.readFileSync(p,'utf8');
const src = read('src/tabelog/scrape/map.py');
const transit = read('docs/transit-layer.js');
const slice = (s,a,b) => s.slice(s.indexOf(a),s.indexOf(b,s.indexOf(a)));
const timers = new Map(); let id=0;
const ctx = vm.createContext({Promise, AbortController, TextEncoder, console:{error(){}},
  setTimeout(fn,ms){timers.set(++id,{fn,ms});return id;}, clearTimeout(i){timers.delete(i);},
  fetch:()=>new Promise(()=>{}), popupsMap:null, popupsPromise:null, popupsUrlForLang:()=>'/popups.json'});
if (!baseline) vm.runInContext(slice(src,'  function fetchBounded(', '  // Trade a Google'),ctx);
vm.runInContext(slice(src,'  function loadPopups()', '  // ===== Apple-style'),ctx);
vm.runInContext('globalThis.p = loadPopups(); p.catch(()=>{});',ctx);
await Promise.resolve();
const popupHasDeadline = [...timers.values()].some(t=>t.ms<=30000);
for (const t of [...timers.values()]) t.fn();
for(let i=0;i<10;i++) await Promise.resolve();
const popupReleased = vm.runInContext('popupsPromise === null',ctx);
timers.clear();
const method = slice(transit,'    _loadLod: function(key)', '    // Switch the active');
vm.runInContext('globalThis.obj = ({' + method + '});',ctx);
vm.runInContext(`Object.assign(obj, {options:{lodUrls:{low:'/lod'}},_lodCache:{},_lodInflight:{},_lodAbort:{},_map:{},fire(){}}); obj._loadLod('low');`,ctx);
await Promise.resolve();
const transitHasDeadline = [...timers.values()].some(t=>t.ms<=30000);
for (const t of [...timers.values()]) t.fn();
for(let i=0;i<10;i++) await Promise.resolve();
const transitReleased = vm.runInContext('!obj._lodInflight.low',ctx);
const result={baseline,popupHasDeadline,popupReleased,transitHasDeadline,transitReleased};
if (!baseline) {
  ctx.fetch=async()=>({ok:true,text:async()=>'{}',json:async()=>({features:[]})});
  await vm.runInContext('loadPopups()',ctx);
  result.popupRetrySucceeded=vm.runInContext('popupsMap !== null',ctx);
  vm.runInContext('obj._parseInto=function(){};obj._targetLodKey=function(){return null;};',ctx);
  await vm.runInContext("obj._loadLod('low')",ctx);
  result.transitRetrySucceeded=vm.runInContext('!!obj._lodCache.low && !obj._lodInflight.low',ctx);
  assert.ok(result.popupRetrySucceeded && result.transitRetrySucceeded);
}
console.log(JSON.stringify(result,null,2));
for(const k of ['popupHasDeadline','popupReleased','transitHasDeadline','transitReleased']) assert.equal(result[k],!baseline,k);
