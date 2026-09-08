// Documents the storage limit the client cannot turn into a CAS guarantee.
import assert from 'node:assert/strict';
import worker from '../../worker/src/index.js';
import {KVMock, makeEnv, signJWT} from '../worker/worker-env.mjs';
const kv = new KVMock({propagationMs:60000,enforceRate:true});
const key = 'state:1001';
kv.seed(key,JSON.stringify({v:1,w:'old',favorites:[],blacklist:[],bookmarks:[]}),'FRA',120000);
await kv.binding('FRA').put(key,JSON.stringify({v:2,w:'A',favorites:['A'],blacklist:[],bookmarks:[]}));
await new Promise(r=>setTimeout(r,1100));
const env=makeEnv(kv,'NRT');
const jwt=await signJWT({sub:'1001',exp:Math.floor(Date.now()/1000)+3600},env.SESSION_HMAC);
const headers={Cookie:'tabelog_session='+jwt,Origin:'https://jpfoodmap.com','Content-Type':'application/json'};
const get=await worker.fetch(new Request('https://api.jpfoodmap.com/api/state',{headers}),env);
assert.equal((await get.json()).v,1);
const put=await worker.fetch(new Request('https://api.jpfoodmap.com/api/state',{method:'PUT',headers,
  body:JSON.stringify({baseV:1,w:'B',favorites:['B'],blacklist:[],bookmarks:[]})}),env);
assert.equal(put.status,200);
assert.deepEqual(JSON.parse(kv.primary(key)).favorites,['B']);
console.log('PASS: offline cross-PoP stale-read model still demonstrates KV has no CAS; strict write serialization is outside this client fix.');
