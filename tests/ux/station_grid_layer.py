#!/usr/bin/env python3
"""Browser acceptance for the 4.3.1a client-side station GridLayer.

The render cases use the generated 8,954-station payload and production badge
renderer. Basemap requests are fulfilled with a deterministic labelled tile
fixture so fractional transform and seam assertions are reproducible. These
screenshots do not claim to exercise CARTO image decode or network behavior.

    .venv-wsl/bin/python tests/ux/station_grid_layer.py --output audit_output/4.3.1a
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Page, Route, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import lib_browser  # noqa: E402


@dataclass(frozen=True)
class Profile:
    width: int
    height: int
    dpr: float
    mobile: bool
    touch: bool


PROFILES = {
    "fold-outer": Profile(475, 751, 2.625, True, True),
    "fold-outer-legacy": Profile(416, 657, 2.625, True, True),
    "dpr2-phone": Profile(416, 657, 2.0, True, True),
    "fold-inner": Profile(932, 704, 2.625, True, True),
    "fold-inner-60": Profile(591, 689, 2.625, True, True),
    "iphone": Profile(402, 874, 3.0, True, True),
    "desktop": Profile(1440, 900, 1.0, False, False),
}

STATION_URL_RE = re.compile(r"/data/stations\.[0-9a-f]{12}\.json(?:\?|$)")
RAIL_URL_RE = re.compile(r"(?:assets\.jpfoodmap\.com|/transit/japan(?:-(?:low|mid))?\.geojson)")
TOKYO = [35.681236, 139.767125]


def seed(stations: str | None = None) -> dict[str, str]:
    values = {
        "tabelog.lang": "zh-CN",
        "tabelog.seenIntro": "1",
        "tabelog.oovHintDismissed": "1",
        "tabelog.syncHintDismissed": "1",
        "tabelog.installHint": json.dumps({"never": True}),
        "tabelog.showTransitLong": "0",
        "tabelog.showTransitCity": "0",
    }
    if stations is not None:
        values["tabelog.showStations"] = stations
    return values


def new_context(browser: Browser, profile: Profile, stations: str | None = None) -> BrowserContext:
    context = browser.new_context(
        viewport={"width": profile.width, "height": profile.height},
        screen={"width": profile.width, "height": profile.height},
        device_scale_factor=profile.dpr,
        is_mobile=profile.mobile,
        has_touch=profile.touch,
        locale="zh-CN",
        service_workers="block",
    )
    values = seed(stations)
    context.add_init_script(
        "(() => { const v=%s; for (const k in v) localStorage.setItem(k,v[k]); })()"
        % json.dumps(values)
    )
    return context


def fixture_tile(route: Route) -> None:
    """A visible coordinate-labelled fixture; alignment proof, not CARTO QA."""
    parts = [p for p in urlparse(route.request.url).path.split("/") if p]
    label = "/".join(parts[-3:]) if len(parts) >= 3 else "fixture"
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256">
      <rect width="256" height="256" fill="#edf1f4"/>
      <path d="M0 0H256V256H0ZM0 128H256M128 0V256M0 0L256 256M256 0L0 256"
            fill="none" stroke="#8da0ad" stroke-width="2"/>
      <text x="12" y="24" font-family="monospace" font-size="15" fill="#34495e">{label}</text>
    </svg>"""
    route.fulfill(status=200, body=svg, content_type="image/svg+xml")


def install_routes(page: Page, fixture_tiles: bool = True) -> tuple[list[str], list[str]]:
    stations: list[str] = []
    rails: list[str] = []

    def seen(request) -> None:
        url = request.url
        if STATION_URL_RE.search(url):
            stations.append(url)
        if RAIL_URL_RE.search(url):
            rails.append(url)

    page.on("request", seen)
    if fixture_tiles:
        page.route("**basemaps.cartocdn.com/**", fixture_tile)
    for host in lib_browser.BLOCKED_HOST_FRAGMENTS:
        page.route(f"**{host}**", lambda route: route.abort())
    page.route("**accounts.google.com/**", lambda route: route.abort())
    return stations, rails


def boot(page: Page, base: str) -> None:
    page.set_default_timeout(90_000)
    page.goto(base + "/index.html", wait_until="domcontentloaded", timeout=90_000)
    lib_browser.wait_ready(page, 90_000)


def go_tokyo(page: Page, zoom: int = 14) -> dict:
    page.evaluate(
        "([center,z]) => MapMod.map.setView(center,z,{animate:false})",
        [TOKYO, zoom],
    )
    page.wait_for_function(
        "() => { const d=MapMod.stationDetail(); return d.loaded && d.total===8954 && d.liveCanvasCount>0; }",
        timeout=60_000,
    )
    page.evaluate("""() => {
      window.__stationQaTransit=Object.values(MapMod.map._layers).find(x=>x&&x._stationGridLayer)||window.__stationQaTransit;
    }""")
    return page.evaluate("() => MapMod.stationDetail()")


def open_layers(page: Page) -> None:
    if not page.locator('[data-ov="layer-toggle"][data-layer="stations"]').count():
        page.locator('[data-fab="layers"]').click()
    page.wait_for_selector('[data-ov="layer-toggle"][data-layer="stations"]')


def assert_canvas_dimensions(page: Page, expected_dpr: float) -> list[dict]:
    rows = page.evaluate("""() => Array.from(document.querySelectorAll('canvas[data-station-tile="1"]')).map(c => {
      const r=c.getBoundingClientRect();
      return {width:c.width,height:c.height,cssWidth:r.width,cssHeight:r.height,
              expectedWidth:Math.round(r.width*MapMod.stationDetail().dpr),
              expectedHeight:Math.round(r.height*MapMod.stationDetail().dpr)};
    })""")
    assert rows, "station renderer produced no non-empty canvas tiles"
    detail = page.evaluate("() => MapMod.stationDetail()")
    assert abs(detail["dpr"] - expected_dpr) < 1e-9, detail
    for row in rows:
        assert row["width"] == row["expectedWidth"], row
        assert row["height"] == row["expectedHeight"], row
    backing = sum(row["width"] * row["height"] * 4 for row in rows)
    assert backing == detail["liveCanvasBytes"], (backing, detail)
    return rows


def assert_fractional_alignment(page: Page) -> dict:
    result = page.evaluate("""() => {
      const map=MapMod.map;
      const transit=Object.values(map._layers).find(x => x && x._stationGridLayer)||window.__stationQaTransit;
      if (!transit) throw new Error('transit layer not found');
      const stations=transit._stationGridLayer;
      const base=Object.values(map._layers).find(x => x instanceof L.TileLayer && x !== stations);
      if (!base) throw new Error('base tile layer not found');
      map._moveStart(true,false);
      map._move(L.latLng(35.6904,139.7427),14.625,{pinch:true,round:false});
      const d=MapMod.stationDetail(), z=d.tileZoom;
      const read=(layer) => {
        const level=layer._levels && layer._levels[z];
        if (!level) return null;
        const t=level.el.style.transform;
        const m=new DOMMatrixReadOnly(getComputedStyle(level.el).transform);
        return {inline:t,matrix:[m.a,m.b,m.c,m.d,m.e,m.f],tileZoom:layer._tileZoom};
      };
      const out={zoom:map.getZoom(),center:[map.getCenter().lat,map.getCenter().lng],
                 detail:d,base:read(base),stations:read(stations)};
      map._move(L.latLng(35.681236,139.767125),14,{pinch:true,round:false});
      map._moveEnd(true);
      return out;
    }""")
    assert abs(result["zoom"] - 14.625) < 1e-9, result
    assert result["base"] and result["stations"], result
    assert result["base"]["tileZoom"] == result["stations"]["tileZoom"] == result["detail"]["tileZoom"], result
    for a, b in zip(result["base"]["matrix"], result["stations"]["matrix"]):
        assert abs(a - b) <= 0.02, result
    return result


def assert_fly_anchor_and_click_priority(page: Page) -> dict:
    page.evaluate("() => MapMod.map.flyTo([35.681236,139.767125],15,{duration:.18})")
    page.wait_for_function("() => !MapMod.map._animatingZoom && MapMod.map.getZoom()===15")
    page.wait_for_timeout(350)
    result = page.evaluate("""() => {
      const map=MapMod.map;
      const transit=Object.values(map._layers).find(x => x && x._stationGridLayer)||window.__stationQaTransit;
      const grid=transit._stationGridLayer;
      const st=transit._allStations.reduce((best,s) => {
        const d=(s.lat-35.681236)**2+(s.lon-139.767125)**2;
        return !best || d<best.d ? {s,d} : best;
      },null).s;
      const p=map.project([st.lat,st.lon],grid._tileZoom), ts=grid.getTileSize();
      let maxAlpha=0, sampled=0;
      Object.values(grid._tiles).forEach(rec => {
        const c=rec.el;
        if (!(c instanceof HTMLCanvasElement)) return;
        const lx=p.x-rec.coords.x*ts.x, ly=p.y-rec.coords.y*ts.y;
        if (lx < -16 || ly < -16 || lx > ts.x+16 || ly > ts.y+16) return;
        const sx=c.width/c.getBoundingClientRect().width;
        const sy=c.height/c.getBoundingClientRect().height;
        const x=Math.max(0,Math.min(c.width-1,Math.round(lx*sx)));
        const y=Math.max(0,Math.min(c.height-1,Math.round(ly*sy)));
        const x0=Math.max(0,x-Math.ceil(8*sx)), y0=Math.max(0,y-Math.ceil(8*sy));
        const x1=Math.min(c.width,x+Math.ceil(8*sx)+1), y1=Math.min(c.height,y+Math.ceil(8*sy)+1);
        const data=c.getContext('2d').getImageData(x0,y0,x1-x0,y1-y0).data;
        for(let i=3;i<data.length;i+=4) maxAlpha=Math.max(maxAlpha,data[i]);
        sampled++;
      });
      document.querySelectorAll('.transit-station-label').forEach(x=>x.remove());
      const cp=map.latLngToContainerPoint([st.lat,st.lon]);
      const lp=map.latLngToLayerPoint([st.lat,st.lon]);
      map.fire('click',{latlng:L.latLng(st.lat,st.lon),containerPoint:cp,layerPoint:lp});
      const normalTip=Array.from(document.querySelectorAll('.transit-station-label')).some(x=>x.textContent.trim().length);
      document.querySelectorAll('.transit-station-label').forEach(x=>x.remove());
      const marker=document.querySelector('.leaflet-marker-icon,.leaflet-interactive,.marker-cluster');
      if (!marker) throw new Error('restaurant/cluster marker target unavailable');
      map.fire('click',{latlng:L.latLng(st.lat,st.lon),containerPoint:cp,layerPoint:lp,
                        originalEvent:{target:marker}});
      const markerTip=Array.from(document.querySelectorAll('.transit-station-label')).some(x=>x.textContent.trim().length);
      map.fire('click',{latlng:map.getBounds().getNorthWest(),containerPoint:L.point(2,2),layerPoint:L.point(2,2)});
      const blankClosed=!Array.from(document.querySelectorAll('.transit-station-label')).some(x=>x.textContent.trim().length);
      map.fire('click',{latlng:L.latLng(st.lat,st.lon),containerPoint:cp,layerPoint:lp});
      map.fire('movestart');
      const moveClosed=!Array.from(document.querySelectorAll('.transit-station-label')).some(x=>x.textContent.trim().length);
      return {station:{name:st.name,lat:st.lat,lon:st.lon},sampled,maxAlpha,
              normalTip,markerTip,blankClosed,moveClosed,containerPoint:[cp.x,cp.y],detail:MapMod.stationDetail()};
    }""")
    assert result["detail"]["tileZoom"] == 15, result
    assert result["sampled"] >= 1 and result["maxAlpha"] > 0, result
    assert result["normalTip"] is True, result
    assert result["markerTip"] is False, result
    assert result["blankClosed"] is True and result["moveClosed"] is True, result
    return result


def assert_boundary_overdraw(page: Page) -> dict:
    """Find a real visible station near a global tile seam and inspect both tiles."""
    result = page.evaluate("""async () => {
      const map=MapMod.map;
      const transit=Object.values(map._layers).find(x => x && x._stationGridLayer)||window.__stationQaTransit;
      const grid=transit._stationGridLayer, z=15, ts=grid.getTileSize();
      const candidate=transit._allStations.map(s => {
        const p=map.project([s.lat,s.lon],z), rx=((p.x%ts.x)+ts.x)%ts.x, ry=((p.y%ts.y)+ts.y)%ts.y;
        return {s,p,edge:Math.min(rx,ts.x-rx,ry,ts.y-ry),rx,ry};
      }).filter(x => x.edge<5 && (x.s.name||'').length>=2)
        .sort((a,b) => (b.s.line_count-a.s.line_count)||a.edge-b.edge)[0];
      if(!candidate) throw new Error('no real station close to a tile boundary');
      map.setView([candidate.s.lat,candidate.s.lon],z,{animate:false});
      await new Promise(resolve => setTimeout(resolve,450));
      const hits=[];
      Object.values(grid._tiles).forEach(rec => {
        const c=rec.el;
        if (!(c instanceof HTMLCanvasElement)) return;
        const p=map.project([candidate.s.lat,candidate.s.lon],z);
        const lx=p.x-rec.coords.x*ts.x, ly=p.y-rec.coords.y*ts.y;
        if(lx < -18 || ly < -18 || lx > ts.x+18 || ly > ts.y+18) return;
        const rect=c.getBoundingClientRect(), sx=c.width/rect.width, sy=c.height/rect.height;
        const x0=Math.max(0,Math.floor((lx-11)*sx)), y0=Math.max(0,Math.floor((ly-11)*sy));
        const x1=Math.min(c.width,Math.ceil((lx+11)*sx)), y1=Math.min(c.height,Math.ceil((ly+11)*sy));
        if(x1<=x0||y1<=y0) return;
        const d=c.getContext('2d').getImageData(x0,y0,x1-x0,y1-y0).data;
        let alpha=0; for(let i=3;i<d.length;i+=4) if(d[i]) alpha++;
        if(alpha) hits.push({coords:[rec.coords.x,rec.coords.y,rec.coords.z],alpha,lx,ly});
      });
      return {station:{id:candidate.s.id,name:candidate.s.name},edge:candidate.edge,
              projected:[candidate.p.x,candidate.p.y],hits,detail:MapMod.stationDetail()};
    }""")
    assert result["edge"] < 5, result
    assert len(result["hits"]) >= 2, (
        "real boundary badge was not painted into both adjacent tiles", result
    )
    return result


def assert_long_label_overdraw(page: Page) -> dict:
    """A real, placed long label crossing a seam must paint into both tiles."""
    result = page.evaluate("""async () => {
      const map=MapMod.map;
      const transit=Object.values(map._layers).find(x=>x&&x._stationGridLayer)||window.__stationQaTransit;
      const grid=transit._stationGridLayer,z=15,ts=grid.getTileSize(),placement=transit._stationPlacement;
      const measure=document.createElement('canvas').getContext('2d');
      measure.font='600 '+transit._stationFontPx()+'px '+transit._stationFont;
      const candidates=[];
      for(const s of transit._allStations){
        const i=s._placementIndex,mask=placement.visibleMaskByItem[i]|0;
        if((mask&2)===0)continue;
        const text=transit._stationName(s),actual=measure.measureText(text).width;
        if(actual<45)continue;
        const p=map.project([s.lat,s.lon],z),rx=((p.x%ts.x)+ts.x)%ts.x;
        const edge=Math.min(rx,ts.x-rx),badge=(s.line_count>=6?22:s.line_count>=3?18:14)*transit._stationStyleScale();
        if(edge>badge/2+4&&edge<actual/2-5)candidates.push({s,p,edge,actual,badge,text});
      }
      candidates.sort((a,b)=>b.actual-a.actual);
      const c=candidates[0]; if(!c)throw new Error('no placed long label crosses a vertical tile seam');
      map.setView([c.s.lat,c.s.lon],z,{animate:false});
      await new Promise(resolve=>setTimeout(resolve,450));
      const p=map.project([c.s.lat,c.s.lon],z),fontPx=transit._stationFontPx();
      const baseline=p.y-c.badge/2-3;
      const globalRect=[p.x-c.actual/2-3,baseline-fontPx-3,p.x+c.actual/2+3,baseline+3];
      const hits=[];
      for(const rec of Object.values(grid._tiles)){
        const tile=rec.el;if(!(tile instanceof HTMLCanvasElement))continue;
        const ox=rec.coords.x*ts.x,oy=rec.coords.y*ts.y;
        const x0=Math.max(0,globalRect[0]-ox),x1=Math.min(ts.x,globalRect[2]-ox);
        const y0=Math.max(0,globalRect[1]-oy),y1=Math.min(ts.y,globalRect[3]-oy);
        if(x1<=x0||y1<=y0)continue;
        const rect=tile.getBoundingClientRect(),sx=tile.width/rect.width,sy=tile.height/rect.height;
        const bx0=Math.max(0,Math.floor(x0*sx)),bx1=Math.min(tile.width,Math.ceil(x1*sx));
        const by0=Math.max(0,Math.floor(y0*sy)),by1=Math.min(tile.height,Math.ceil(y1*sy));
        const data=tile.getContext('2d').getImageData(bx0,by0,bx1-bx0,by1-by0).data;
        let alpha=0;for(let i=3;i<data.length;i+=4)if(data[i])alpha++;
        if(alpha)hits.push({coords:[rec.coords.x,rec.coords.y,rec.coords.z],alpha});
      }
      return {station:{id:c.s.id,name:c.text},actualWidth:c.actual,edge:c.edge,hits,
              detail:MapMod.stationDetail()};
    }""")
    assert len(result["hits"]) >= 2, (
        "real placed long label did not paint into both adjacent tiles", result
    )
    return result


def assert_payload_measurement_and_collisions(page: Page, payload: dict) -> dict:
    """Check generated max130 envelopes in a real browser, then all six zooms."""
    compact = {
        "rows": payload["stations"],
        "profiles": payload["placement"]["profiles"],
    }
    result = page.evaluate("""data => {
      const rows=data.rows, profiles=data.profiles;
      const font='600 14.3px system-ui,-apple-system,"Hiragino Sans","Noto Sans CJK JP",sans-serif';
      const ctx=document.createElement('canvas').getContext('2d'); ctx.font=font;
      const measured={};
      for(const [profileKey,p] of Object.entries(profiles)){
        const en=profileKey.startsWith('en-'), violations=[];
        let maxSlack=-Infinity, maxActual=0;
        for(let i=0;i<rows.length;i++){
          const text=en?(rows[i][3]||rows[i][2]):rows[i][2];
          const actual=ctx.measureText(text).width, envelope=p.labelWidthByItem[i];
          maxActual=Math.max(maxActual,actual); maxSlack=Math.max(maxSlack,actual-envelope);
          if(actual>envelope+.05 && violations.length<20)
            violations.push({i,id:rows[i][6],text,actual,envelope});
        }
        measured[profileKey]={font,maxActual,maxActualMinusEnvelope:maxSlack,violations};
      }

      function project(lon,lat,z){
        const sin=Math.sin(lat*Math.PI/180), scale=256*Math.pow(2,z);
        return [scale*(lon/360+.5),scale*(.5-Math.log((1+sin)/(1-sin))/(4*Math.PI))];
      }
      function overlaps(a,b){return a[0]<b[2]&&a[2]>b[0]&&a[1]<b[3]&&a[3]>b[1];}
      function add(hash,r,item){
        const C=64;
        for(let x=Math.floor(r[0]/C);x<=Math.floor(r[2]/C);x++)
          for(let y=Math.floor(r[1]/C);y<=Math.floor(r[3]/C);y++){
            const k=x+','+y; if(!hash.has(k))hash.set(k,[]); hash.get(k).push({r,item});
          }
      }
      function hits(hash,r){
        const C=64,out=[],seen=new Set();
        for(let x=Math.floor(r[0]/C);x<=Math.floor(r[2]/C);x++)
          for(let y=Math.floor(r[1]/C);y<=Math.floor(r[3]/C);y++){
            for(const v of hash.get(x+','+y)||[]){
              const key=v.item.kind+':'+v.item.i;
              if(!seen.has(key)&&overlaps(r,v.r)){seen.add(key);out.push(v.item);}
            }
          }
        return out;
      }
      const collision={};
      for(const [profileKey,p] of Object.entries(profiles)){
        const zooms={};
        for(let z=14;z<=19;z++){
          const hash=new Map(), labels=[], violations=[];
          for(let i=0;i<rows.length;i++){
            const row=rows[i], pt=project(row[0],row[1],z), size=row[5]>=6?22:row[5]>=3?18:14;
            if(z>14||row[5]>=3){
              const r=[pt[0]-size/2-2,pt[1]-size/2-2,pt[0]+size/2+2,pt[1]+size/2+2];
              add(hash,r,{kind:'badge',i,id:row[6]});
            }
            if((p.visibleMaskByItem[i]&(1<<(z-14)))!==0){
              const baseline=pt[1]-size/2-3,w=p.labelWidthByItem[i];
              labels.push({i,id:row[6],r:[pt[0]-w/2-3,baseline-14.3-3,pt[0]+w/2+3,baseline+3]});
            }
          }
          for(const label of labels){
            const conflicts=hits(hash,label.r).filter(x=>x.kind!=='badge'||x.i!==label.i);
            if(conflicts.length&&violations.length<20)
              violations.push({label:{i:label.i,id:label.id},conflicts});
            add(hash,label.r,{kind:'label',i:label.i,id:label.id});
          }
          zooms[z]={labels:labels.length,violations};
        }
        collision[profileKey]=zooms;
      }
      return {measured,collision};
    }""", compact)
    for profile, measured in result["measured"].items():
        assert not measured["violations"], (profile, measured)
    for profile, zooms in result["collision"].items():
        for zoom, row in zooms.items():
            assert not row["violations"], (profile, zoom, row)
    return result


def assert_drag_label_stability_and_refresh(page: Page) -> dict:
    result = page.evaluate("""async () => {
      const map=MapMod.map, wait=ms=>new Promise(r=>setTimeout(r,ms));
      const transit=Object.values(map._layers).find(x=>x&&x._stationGridLayer)||window.__stationQaTransit, grid=transit._stationGridLayer;
      map.setView([35.681236,139.767125],15,{animate:false}); await wait(250);
      const snapshot=()=>{
        const out={}; for(const [key,rec] of Object.entries(grid._tiles))
          if(rec.el instanceof HTMLCanvasElement) out[key]=rec.el.toDataURL();
        return out;
      };
      const before=snapshot(), d0=MapMod.stationDetail();
      map.panBy([140,70],{animate:false}); await wait(250);
      const after=snapshot(), d1=MapMod.stationDetail();
      const common=Object.keys(before).filter(k=>k in after), changed=common.filter(k=>before[k]!==after[k]);
      const redraw0=d1.counters.redraw;
      window.dispatchEvent(new Event('resize')); await wait(250);
      const d2=MapMod.stationDetail();
      transit.setStationProfile({key:d2.profileKey,uiDensity:1,fontScale:115}); await wait(250);
      const d3=MapMod.stationDetail();
      return {commonTiles:common.length,changedTiles:changed,
        tileCreateDelta:d1.counters.tileCreate-d0.counters.tileCreate,
        note:'new-view pan may execute createTile JavaScript',before:d0,afterPan:d1,afterRefresh:d2,
        resizeRedrawDelta:d2.counters.redraw-redraw0,
        profileRedrawDelta:d3.counters.redraw-d2.counters.redraw};
    }""")
    assert result["commonTiles"] > 0, result
    assert not result["changedTiles"], result
    assert result["afterPan"]["profileKey"] == result["before"]["profileKey"], result
    assert result["resizeRedrawDelta"] == 0, result
    assert result["profileRedrawDelta"] == 1, result
    return result


def assert_cycles_bounded(page: Page) -> dict:
    result = page.evaluate("""async () => {
      const wait=ms=>new Promise(r=>setTimeout(r,ms));
      const samples=[];
      for(let i=0;i<6;i++){
        App.act.setLayers({stations:false}); await wait(80);
        const off=MapMod.stationDetail();
        if(off.liveCanvasCount!==0||off.liveCanvasBytes!==0) throw new Error('off retained station canvas');
        App.act.setLayers({stations:true});
        MapMod.map.setZoom(i%2?14:15,{animate:false});
        const deadline=performance.now()+5000;
        while(performance.now()<deadline){
          const d=MapMod.stationDetail(); if(d.loaded&&d.liveCanvasCount>0)break;
          await wait(50);
        }
        if(!MapMod.stationDetail().loaded)throw new Error('station payload did not recover during toggle cycle');
        samples.push(MapMod.stationDetail());
      }
      MapMod.map.setZoom(14,{animate:false}); await wait(250);
      return {samples,final:MapMod.stationDetail()};
    }""")
    counts = [x["liveCanvasCount"] for x in result["samples"]]
    sizes = [x["liveCanvasBytes"] for x in result["samples"]]
    assert result["final"]["liveCanvasCount"] <= max(counts), result
    assert result["final"]["liveCanvasBytes"] <= max(sizes), result
    assert result["final"]["loaded"] and not result["final"]["loading"], result
    assert max(counts) < 100, result
    assert max(sizes) < 100 * 256 * 256 * 4 * 9, result
    return result


def run_state_and_network(browser: Browser, base: str, payload_text: str) -> dict:
    report: dict[str, object] = {}

    # Default-on remains dormant below z12 and station-only never requests rail.
    ctx = new_context(browser, PROFILES["desktop"], None)
    page = ctx.new_page()
    station_requests, rail_requests = install_routes(page)
    boot(page, base)
    assert MapZoom(page) < 12
    page.wait_for_timeout(350)
    assert not station_requests, station_requests
    assert not rail_requests, rail_requests
    detail_low = page.evaluate("() => MapMod.stationDetail()")
    detail_loaded = go_tokyo(page)
    assert len(station_requests) == 1, station_requests
    assert not rail_requests, rail_requests
    assert detail_loaded["renderer"] == "grid", detail_loaded
    report["default_low_zoom"] = detail_low
    report["station_only"] = {"detail": detail_loaded, "stationRequests": len(station_requests), "railRequests": 0}

    open_layers(page)
    switch = page.locator('[data-ov="layer-toggle"][data-layer="stations"]')
    assert switch.get_attribute("role") == "switch"
    assert switch.get_attribute("aria-checked") == "true"
    switch.click()
    page.wait_for_function("() => App.state.layers.stations===false")
    assert page.evaluate("localStorage.getItem('tabelog.showStations')") == "0"
    page.reload(wait_until="domcontentloaded")
    lib_browser.wait_ready(page, 90_000)
    assert page.evaluate("App.state.layers.stations") is False
    page.evaluate("() => App.act.setLayers({long:true,stations:false})")
    assert page.evaluate("App.state.layers.long && App.state.layers.stations") is True
    open_layers(page)
    assert switch.is_disabled()
    page.evaluate("() => App.act.setLayers({long:false})")
    assert page.evaluate("!App.state.layers.long && App.state.layers.stations") is True
    report["state"] = {"explicitOffPersisted": True, "railCoupling": True}
    ctx.close()

    # Explicit off at z14 fetches neither station nor rail payload.
    ctx = new_context(browser, PROFILES["desktop"], "0")
    page = ctx.new_page()
    station_requests, rail_requests = install_routes(page)
    boot(page, base)
    page.evaluate("() => MapMod.map.setView([35.681236,139.767125],14,{animate:false})")
    page.wait_for_timeout(500)
    assert not station_requests and not rail_requests, (station_requests, rail_requests)
    report["explicit_off_network"] = {"stationRequests": 0, "railRequests": 0}
    ctx.close()

    # First station request fails; the visible retry control recovers real data.
    ctx = new_context(browser, PROFILES["desktop"], "1")
    page = ctx.new_page()
    attempts = {"n": 0}

    def retry_route(route: Route) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            route.fulfill(status=503, body="temporary fixture failure", content_type="text/plain")
        else:
            route.fulfill(status=200, body=payload_text, content_type="application/json")

    page.route("**/data/stations.*.json", retry_route)
    install_routes(page)
    boot(page, base)
    page.evaluate("() => MapMod.map.setView([35.681236,139.767125],14,{animate:false})")
    page.wait_for_function("() => App.state.layers.error.stations===true")
    assert page.evaluate("App.state.layers.stations") is True
    open_layers(page)
    retry = page.locator('[data-ov="layer-retry"][data-layer="stations"]')
    assert retry.count() == 1
    retry.click()
    page.wait_for_function("() => MapMod.stationDetail().loaded && MapMod.stationDetail().total===8954")
    assert attempts["n"] == 2, attempts
    report["retry"] = {"attempts": attempts["n"], "recovered": True}
    ctx.close()

    # The supplied PNG is lazy, falls back safely, and retries through the
    # same station retry action without creating per-station requests.
    ctx = new_context(browser, PROFILES["desktop"], "1")
    page = ctx.new_page()
    icon_attempts = {"n": 0}
    icon_bytes = (ROOT / "docs/img/station-icon-v2.png").read_bytes()

    def icon_retry_route(route: Route) -> None:
        icon_attempts["n"] += 1
        if icon_attempts["n"] == 1:
            route.fulfill(status=503, body=b"temporary icon failure")
        else:
            route.fulfill(status=200, body=icon_bytes, content_type="image/png")

    page.route("**/img/station-icon-v2.png*", icon_retry_route)
    install_routes(page)
    boot(page, base)
    page.evaluate("() => MapMod.map.setView([35.681236,139.767125],14,{animate:false})")
    page.wait_for_function("() => MapMod.stationDetail().badgeImageStatus==='error'")
    fallback = page.evaluate("() => MapMod.stationDetail()")
    page.evaluate("() => MapMod.retryStations()")
    page.wait_for_function("() => MapMod.stationDetail().badgeImageStatus==='ready'")
    recovered = page.evaluate("() => MapMod.stationDetail()")
    assert icon_attempts["n"] == 2, (icon_attempts, fallback, recovered)
    assert fallback["liveCanvasCount"] > 0 and recovered["liveCanvasCount"] > 0
    report["badgeImageRetry"] = {
        "attempts": icon_attempts["n"], "fallbackRendered": True, "recovered": True
    }
    ctx.close()

    # A superseded request may settle after its replacement. Its finally/catch
    # must not overwrite the replacement's loading, error, or parsed data.
    race_results = {}
    for race_kind in ("off-on", "retry-inflight", "no-abort-off-on"):
        ctx = new_context(browser, PROFILES["desktop"], "1")
        if race_kind == "no-abort-off-on":
            ctx.add_init_script("Object.defineProperty(window,'AbortController',{value:undefined,writable:true,configurable:true})")
        ctx.add_init_script("""(() => {
          const nativeFetch=window.fetch.bind(window); let attempts=0;
          window.__stationRaceAttempts=()=>attempts;
          window.fetch=function(input,init){
            const url=String(input&&input.url||input);
            if(!/data\\/stations\\.[0-9a-f]{12}\\.json/.test(url))
              return nativeFetch(input,init);
            attempts++;
            if(attempts!==1) return nativeFetch(input,init);
            return new Promise((resolve,reject)=>setTimeout(()=>{
              nativeFetch(input,init).then(resolve,reject);
            },650));
          };
        })()""")
        page = ctx.new_page()
        install_routes(page)
        boot(page, base)
        page.evaluate("() => MapMod.map.setView([35.681236,139.767125],14,{animate:false})")
        page.wait_for_function("() => window.__stationRaceAttempts()>=1")
        if race_kind in ("off-on", "no-abort-off-on"):
            page.evaluate("() => App.act.setLayers({stations:false})")
            page.wait_for_function("""() => {
              const d=MapMod.stationDetail(),s=MapMod.stationStatus();
              return !d.visible&&!d.attached&&!d.loading&&
                App.state.layers.loading.stations===false&&
                App.state.layers.error.stations===false&&
                s.status==='idle'&&s.visible===false;
            }""")
            page.wait_for_timeout(120)
            off_detail = page.evaluate("""() => ({detail:MapMod.stationDetail(),status:MapMod.stationStatus(),
              app:{loading:App.state.layers.loading.stations,error:App.state.layers.error.stations}})""")
            assert not off_detail["detail"]["loading"], off_detail
            assert off_detail["status"]["status"] == "idle", off_detail
            page.evaluate("() => App.act.setLayers({stations:true})")
        else:
            page.evaluate("() => MapMod.retryStations()")
        page.wait_for_function("() => window.__stationRaceAttempts()>=2")
        page.wait_for_function("() => MapMod.stationDetail().loaded && MapMod.stationDetail().total===8954")
        page.wait_for_timeout(900)
        detail = page.evaluate("() => MapMod.stationDetail()")
        attempts = page.evaluate("window.__stationRaceAttempts()")
        assert attempts >= 2, (race_kind, attempts, detail)
        assert detail["loaded"] and not detail["loading"] and not detail["error"], (race_kind, detail)
        assert detail["total"] == 8954 and detail["liveCanvasCount"] > 0, (race_kind, detail)
        app_status = page.evaluate("""() => ({station:MapMod.stationStatus(),
          loading:App.state.layers.loading.stations,error:App.state.layers.error.stations})""")
        assert app_status["station"]["status"] == "ok" and not app_status["loading"] and not app_status["error"], app_status
        race_results[race_kind] = {"attempts": attempts, "final": detail, "appStatus": app_status}
        ctx.close()
    report["inflightRaces"] = race_results
    return report


def MapZoom(page: Page) -> float:
    return float(page.evaluate("() => MapMod.map.getZoom()"))


def run_profile(browser: Browser, base: str, name: str, profile: Profile, output: Path,
                payload: dict) -> dict:
    ctx = new_context(browser, profile, "1")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    station_requests, rail_requests = install_routes(page)
    boot(page, base)
    initial = go_tokyo(page)
    assert not rail_requests, rail_requests
    assert len(station_requests) == 1, station_requests
    dims = assert_canvas_dimensions(page, min(max(profile.dpr, 1), 3))
    alignment = assert_fractional_alignment(page)
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"station-grid-fixture-{name}.png"), full_page=False)
    anchor = assert_fly_anchor_and_click_priority(page) if name == "desktop" else None
    boundary = assert_boundary_overdraw(page) if name == "fold-outer" else None
    long_label_boundary = assert_long_label_overdraw(page) if name == "fold-outer" else None
    payload_geometry = assert_payload_measurement_and_collisions(page, payload) if name == "desktop" else None
    drag_stability = assert_drag_label_stability_and_refresh(page) if name == "fold-outer" else None
    cycles = assert_cycles_bounded(page) if name == "fold-outer" else None
    page.evaluate("() => MapMod.map.setView([35.681236,139.767125],14,{animate:false})")
    page.wait_for_timeout(250)
    assert not errors, errors
    final = page.evaluate("() => MapMod.stationDetail()")
    ctx.close()
    return {
        "profile": profile.__dict__,
        "basemap": "deterministic labelled SVG tile fixture",
        "payload": {"stationRequests": len(station_requests), "railRequests": len(rail_requests)},
        "initial": initial,
        "canvasDimensions": dims,
        "fractionalAlignment": alignment,
        "flyAnchorAndClickPriority": anchor,
        "boundaryOverdraw": boundary,
        "longLabelBoundaryOverdraw": long_label_boundary,
        "cycles": cycles,
        "payloadMeasurementAndCollisions": payload_geometry,
        "dragLabelStabilityAndRefresh": drag_stability,
        "final": final,
    }


def capture_real_basemap(browser: Browser, base: str, output: Path) -> dict:
    """Best-effort visual evidence. Never exposes or serializes tile URLs."""
    profile = PROFILES["fold-outer"]
    ctx = new_context(browser, profile, "1")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    # Keep quota-bearing/non-map third parties blocked; CARTO is intentional.
    for host in lib_browser.BLOCKED_HOST_FRAGMENTS:
        page.route(f"**{host}**", lambda route: route.abort())
    page.route("**accounts.google.com/**", lambda route: route.abort())
    try:
        boot(page, base)
        detail = go_tokyo(page)
        page.wait_for_function(
            "() => Array.from(document.querySelectorAll('.leaflet-tile-pane img.leaflet-tile-loaded')).some(x=>x.naturalWidth>1)",
            timeout=30_000,
        )
        tiles = page.evaluate("""() => {
          const all=Array.from(document.querySelectorAll('.leaflet-tile-pane img.leaflet-tile-loaded'));
          return {loaded:all.length,nontrivial:all.filter(x=>x.naturalWidth>1&&x.naturalHeight>1).length};
        }""")
        output.mkdir(parents=True, exist_ok=True)
        path = output / "station-grid-real-basemap-fold-outer.png"
        page.screenshot(path=str(path), full_page=False)
        result = {"available": True, "screenshot": path.name, "tiles": tiles, "detail": detail,
                  "note": "real configured basemap; request URLs and key omitted"}
    except Exception as error:
        result = {"available": False, "error": type(error).__name__,
                  "note": "real basemap was unavailable; fixture evidence remains separate"}
    ctx.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", type=Path, default=ROOT / "docs")
    parser.add_argument("--output", type=Path, default=ROOT / "audit_output" / "4.3.1a")
    parser.add_argument("--profiles", default=",".join(PROFILES),
                        help="comma-separated: " + ",".join(PROFILES))
    parser.add_argument("--skip-state", action="store_true")
    parser.add_argument("--real-basemap-screenshot", action="store_true")
    parser.add_argument("--report-name", default="station-grid-acceptance.json")
    parser.add_argument("--browser", choices=("chromium", "webkit"), default="chromium")
    args = parser.parse_args()
    selected = [x.strip() for x in args.profiles.split(",") if x.strip()]
    unknown = sorted(set(selected) - set(PROFILES))
    if unknown:
        parser.error(f"unknown profiles: {unknown}")

    docs = args.docs.resolve()
    lib_browser.DOCS = docs
    html = (docs / "index.html").read_text(encoding="utf-8")
    names = sorted(set(re.findall(r"data/(stations\.[0-9a-f]{12}\.json)", html)))
    payloads = [docs / "data" / names[0]] if len(names) == 1 else []
    if len(payloads) != 1:
        raise AssertionError(f"expected one station payload, found {payloads}")
    payload_text = payloads[0].read_text(encoding="utf-8")
    payload = json.loads(payload_text)
    if len(payload.get("stations", [])) != 8954:
        raise AssertionError("acceptance requires the real 8,954-station payload")

    report: dict[str, object] = {
        "schema": 1,
        "scope": "local headless Chromium; no Fold hardware performance claim",
        "docs": str(docs),
        "stationPayload": payloads[0].name,
        "renderer": {
            "sha256": hashlib.sha256((docs / "transit-layer.js").read_bytes()).hexdigest(),
            "md5": hashlib.md5((docs / "transit-layer.js").read_bytes()).hexdigest(),
            "indexAssetVersionMd5Prefix": (re.search(r"transit-layer\.js\?v=([0-9a-f]+)", html) or [None, None])[1],
        },
        "profiles": {},
    }
    with lib_browser.serve_docs(8991) as base, sync_playwright() as playwright:
        browser = getattr(playwright, args.browser).launch()
        if not args.skip_state:
            report["stateAndNetwork"] = run_state_and_network(browser, base, payload_text)
        for name in selected:
            print(f"station-grid UX: {name}", flush=True)
            report["profiles"][name] = run_profile(browser, base, name, PROFILES[name], args.output, payload)
        if args.real_basemap_screenshot:
            print("station-grid UX: real configured basemap screenshot", flush=True)
            report["realBasemap"] = capture_real_basemap(browser, base, args.output)
        report["browserVersion"] = browser.version
        browser.close()

    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output / args.report_name
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"station_grid_layer.py OK: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
