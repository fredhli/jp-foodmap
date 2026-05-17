"""
Fetch OSM rail + station data for a region via Overpass and dump GeoJSON.

  uv run python src/tabelog/scrape/fetch_transit.py --region okayama

Bounding boxes live in REGIONS below. Output lands in docs/transit/{region}.geojson
so the static site can fetch() it directly — no tile server, no API key.

Skips industrial/yard/spur tracks so we only get passenger lines + subway/tram.
"""

import argparse
import json
from pathlib import Path

import httpx

# Mirrors tried in order — the main overpass-api.de is often overloaded.
# private.coffee tends to respond fastest from outside Europe.
OVERPASS_URLS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# (south, west, north, east)
REGIONS: dict[str, tuple[float, float, float, float]] = {
    "okayama": (34.55, 133.78, 34.82, 134.12),
}

QUERY = """
[out:json][timeout:180];
(
  way["railway"="rail"]["usage"!="industrial"]["service"!~"^(yard|spur|siding|crossover)$"]({s},{w},{n},{e});
  way["railway"~"^(light_rail|subway|tram|monorail|narrow_gauge)$"]({s},{w},{n},{e});
  node["railway"~"^(station|tram_stop|halt)$"]({s},{w},{n},{e});
);
out geom;
"""


def to_geojson(osm: dict) -> dict:
    features = []
    for el in osm.get("elements", []):
        if el["type"] == "way" and el.get("geometry"):
            tags = el.get("tags", {})
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[g["lon"], g["lat"]] for g in el["geometry"]],
                },
                "properties": {
                    "name": tags.get("name"),
                    "name_en": tags.get("name:en"),
                    "operator": tags.get("operator"),
                    "ref": tags.get("ref"),
                    "railway": tags.get("railway"),
                    "colour": tags.get("colour"),
                },
            })
        elif el["type"] == "node":
            tags = el.get("tags", {})
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [el["lon"], el["lat"]]},
                "properties": {
                    "name": tags.get("name"),
                    "name_en": tags.get("name:en"),
                    "operator": tags.get("operator"),
                    "railway": tags.get("railway"),
                },
            })
    return {"type": "FeatureCollection", "features": features}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="okayama", choices=list(REGIONS))
    ap.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[3] / "docs" / "transit",
    )
    args = ap.parse_args()

    s, w, n, e = REGIONS[args.region]
    q = QUERY.format(s=s, w=w, n=n, e=e)
    print(f"querying Overpass for {args.region} bbox=({s},{w},{n},{e})")
    osm = None
    last_err: Exception | None = None
    for url in OVERPASS_URLS:
        print(f"  try {url}")
        try:
            resp = httpx.post(url, data={"data": q}, timeout=200.0)
            resp.raise_for_status()
            osm = resp.json()
            break
        except (httpx.HTTPError, ValueError) as e:
            print(f"    failed: {e}")
            last_err = e
    if osm is None:
        raise SystemExit(f"all Overpass mirrors failed; last error: {last_err}")
    print(f"  raw elements: {len(osm.get('elements', []))}")

    gj = to_geojson(osm)
    n_ways = sum(1 for f in gj["features"] if f["geometry"]["type"] == "LineString")
    n_nodes = sum(1 for f in gj["features"] if f["geometry"]["type"] == "Point")
    print(f"  {n_ways} lines, {n_nodes} stations")

    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / f"{args.region}.geojson"
    out_path.write_text(json.dumps(gj, ensure_ascii=False, separators=(",", ":")))
    size_kb = out_path.stat().st_size / 1024
    print(f"  wrote {out_path} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
