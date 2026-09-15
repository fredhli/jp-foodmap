"""
Extract passenger rail + stations from a Geofabrik OSM PBF and dump GeoJSON,
enriching each way with its parent route relation's colour/name when present.

  uv run python src/tabelog/scrape/extract_japan_transit.py

OSM models lines two ways:
  - Way:      a single track segment (railway=rail, name=JR山手線, ...)
  - Relation: the whole numbered/named line (type=route, route=train,
              colour=#9ACD32, name=山手線), with the segment ways as members.

The relation usually carries the official line colour. We pull both, then
join: way.route_colour := its parent route relation's colour.

Pipeline:
  1. pyosmium tags-filter PBF -> japan-rail.osm.pbf    (ways/stations only)
  2. pyosmium tags-filter PBF -> japan-routes.osm.pbf  (route relations only)
  3. pyosmium cat .pbf        -> japan-routes.opl      (text for relations)
  4. pyosmium export .pbf     -> japan-rail.geojson    (with way IDs)
  5. python                    - parse OPL into way_id -> route info
                               - walk GeoJSON, augment, slim, round coords
                               - write docs/transit/japan.geojson

All four PBF/OPL/GeoJSON steps run through pyosmium (the Python binding to
libosmium). No system osmium-tool needed — wheels include libosmium itself,
so a plain `uv sync` is enough on every platform.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import osmium

REPO = Path(__file__).resolve().parents[3]
OSM_DIR = REPO / "data" / "osm"
DOCS_TRANSIT = REPO / "docs" / "transit"

RAW_PBF = OSM_DIR / "japan-latest.osm.pbf"
FILTERED_PBF = OSM_DIR / "japan-rail.osm.pbf"
ROUTES_PBF = OSM_DIR / "japan-routes.osm.pbf"
ROUTES_OPL = OSM_DIR / "japan-routes.opl"
RAW_GEOJSON = OSM_DIR / "japan-rail.geojson"
FINAL_GEOJSON = DOCS_TRANSIT / "japan.geojson"

KEEP_WAY_RAILWAY = {"rail", "light_rail", "subway", "tram", "monorail", "narrow_gauge"}
KEEP_NODE_RAILWAY = {"station", "tram_stop", "halt"}
KEEP_ROUTE_TYPES = {"train", "subway", "tram", "light_rail", "monorail", "railway"}

SKIP_USAGE = {"industrial", "military", "tourism"}
SKIP_SERVICE = {"yard", "spur", "siding", "crossover"}

# Merge same-name stations within this distance (approx. Tokyo-lat meters).
# 600m comfortably handles "Tokyo Station" / "Otemachi" complexes whose
# subway and JR entrances can be ~500m apart, while still keeping name-collision
# pairs in different cities separate (those are tens of km apart minimum).
STATION_MERGE_M = 600.0

# Douglas-Peucker tolerance in degrees. At Tokyo latitude:
#   0.00001° ≈ 1m  (about 1 pixel at zoom 17, sub-pixel at lower zooms)
# Generous reduction without visible quality loss in normal use.
SIMPLIFY_TOL_DEG = 1.0e-5
SIMPLIFY_TOL_SQ = SIMPLIFY_TOL_DEG ** 2


def step_filter_ways() -> None:
    """Strip the raw PBF down to railway ways + station/halt/tram_stop nodes.
    BackReferenceWriter pulls in the nodes referenced by each kept way so the
    geometry survives — matches what `osmium tags-filter` does by default."""
    if FILTERED_PBF.exists() and FILTERED_PBF.stat().st_mtime > RAW_PBF.stat().st_mtime:
        print(f"  (skip) {FILTERED_PBF.name} up to date")
        return
    print(f"  filtering {RAW_PBF.name} -> {FILTERED_PBF.name}")
    OSM_DIR.mkdir(parents=True, exist_ok=True)
    src = str(RAW_PBF)
    n_ways = 0
    n_nodes = 0
    with osmium.BackReferenceWriter(
            str(FILTERED_PBF), ref_src=src, overwrite=True) as writer:
        for obj in osmium.FileProcessor(src):
            if obj.is_way():
                if obj.tags.get("railway") in KEEP_WAY_RAILWAY:
                    writer.add(obj)
                    n_ways += 1
            elif obj.is_node():
                if obj.tags.get("railway") in KEEP_NODE_RAILWAY:
                    writer.add(obj)
                    n_nodes += 1
    print(f"  kept {n_ways} ways + {n_nodes} stations "
          f"(+ ways' referenced nodes via back-ref)")


def step_filter_routes() -> None:
    """Keep only route relations themselves — no referenced members. We
    only need their tags + member-id lists, which OPL gives us."""
    if ROUTES_PBF.exists() and ROUTES_PBF.stat().st_mtime > RAW_PBF.stat().st_mtime:
        print(f"  (skip) {ROUTES_PBF.name} up to date")
        return
    print(f"  filtering {RAW_PBF.name} -> {ROUTES_PBF.name}")
    OSM_DIR.mkdir(parents=True, exist_ok=True)
    n_rels = 0
    with osmium.SimpleWriter(str(ROUTES_PBF), overwrite=True) as writer:
        for obj in osmium.FileProcessor(str(RAW_PBF)):
            if not obj.is_relation():
                continue
            if obj.tags.get("type") != "route":
                continue
            if obj.tags.get("route") not in KEEP_ROUTE_TYPES:
                continue
            writer.add(obj)
            n_rels += 1
    print(f"  kept {n_rels} route relations")


def step_dump_routes_opl() -> None:
    """SimpleWriter dispatches output format from the file extension —
    `.opl` here, vs `.osm.pbf` for the filter steps."""
    if ROUTES_OPL.exists() and ROUTES_OPL.stat().st_mtime > ROUTES_PBF.stat().st_mtime:
        print(f"  (skip) {ROUTES_OPL.name} up to date")
        return
    print(f"  dumping {ROUTES_PBF.name} -> {ROUTES_OPL.name}")
    with osmium.SimpleWriter(str(ROUTES_OPL), overwrite=True) as writer:
        for obj in osmium.FileProcessor(str(ROUTES_PBF)):
            writer.add(obj)


def step_export() -> None:
    """FILTERED_PBF -> GeoJSON. with_locations() caches node coords so way
    iteration materializes a LineString geometry; for the ~12MB filtered
    railway PBF that's only a few MB of RAM. Feature IDs match what
    `osmium export --add-unique-id=type_id` would emit: "n12345" for
    nodes, "w12345" for ways — the downstream postprocess parses that
    prefix to recover the way ID."""
    if RAW_GEOJSON.exists() and RAW_GEOJSON.stat().st_mtime > FILTERED_PBF.stat().st_mtime:
        print(f"  (skip) {RAW_GEOJSON.name} up to date")
        return
    print(f"  exporting {FILTERED_PBF.name} -> {RAW_GEOJSON.name}")
    factory = osmium.geom.GeoJSONFactory()
    fp = osmium.FileProcessor(str(FILTERED_PBF)).with_locations()
    n_points = 0
    n_lines = 0
    n_skipped = 0
    with RAW_GEOJSON.open("w", encoding="utf-8") as fh:
        fh.write('{"type":"FeatureCollection","features":[\n')
        first = True
        for obj in fp:
            if obj.is_node():
                # Back-ref nodes (added by the filter step so ways have
                # vertices) carry no tags themselves — skip them so the
                # geojson only holds stations + line geometry.
                if not obj.tags:
                    continue
                try:
                    geom_str = factory.create_point(obj.location)
                except osmium.InvalidLocationError:
                    n_skipped += 1
                    continue
                fid = f"n{obj.id}"
                n_points += 1
            elif obj.is_way():
                try:
                    geom_str = factory.create_linestring(obj)
                except (osmium.InvalidLocationError, RuntimeError):
                    n_skipped += 1
                    continue
                fid = f"w{obj.id}"
                n_lines += 1
            else:
                continue
            props = {k: v for k, v in obj.tags}
            props["@id"] = fid
            ftr = {
                "type": "Feature",
                "id": fid,
                "geometry": json.loads(geom_str),
                "properties": props,
            }
            if not first:
                fh.write(",\n")
            json.dump(ftr, fh, ensure_ascii=False)
            first = False
        fh.write("\n]}\n")
    skip_note = f", skipped {n_skipped} with invalid geometry" if n_skipped else ""
    print(f"  wrote {n_points} points + {n_lines} lines{skip_note}")


_OPL_ESCAPE = re.compile(r"%([0-9a-fA-F]{1,6})%")


def opl_decode(s: str) -> str:
    """OPL escapes every char outside 0x21–0x7E (and the delimiters , = @ % ")
    as %XXXX% with a variable-length hex codepoint — NOT standard URL encoding."""
    return _OPL_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), s)


def parse_route_relations(opl_path: Path):
    """Yield (rel_tags_dict, list_of_way_ids) for each route relation."""
    with opl_path.open(encoding="utf-8") as f:
        for line in f:
            if not line or not line.startswith("r"):
                continue
            tags: dict[str, str] = {}
            way_ids: list[int] = []
            for tok in line.rstrip("\n").split(" "):
                if tok.startswith("T") and len(tok) > 1:
                    for kv in tok[1:].split(","):
                        if "=" in kv:
                            k, v = kv.split("=", 1)
                            tags[opl_decode(k)] = opl_decode(v)
                elif tok.startswith("M") and len(tok) > 1:
                    for m in tok[1:].split(","):
                        mref = m.split("@", 1)[0] if "@" in m else m
                        if mref.startswith("w"):
                            try:
                                way_ids.append(int(mref[1:]))
                            except ValueError:
                                pass
            yield tags, way_ids


def build_way_route_map(opl_path: Path) -> dict[int, dict]:
    """way_id -> {route_colour?, route_name?, route_ref?, route_operator?}.

    A way may belong to several route relations (e.g. local + rapid trains
    share the track). When merging, we keep the first colour we see but
    accumulate names so the most-specific naming wins."""
    print(f"  parsing {opl_path}")
    way_map: dict[int, dict] = {}
    n_rels = 0
    n_with_colour = 0
    for tags, way_ids in parse_route_relations(opl_path):
        if tags.get("type") != "route":
            continue
        if tags.get("route") not in KEEP_ROUTE_TYPES:
            continue
        n_rels += 1
        info: dict[str, str] = {}
        if c := tags.get("colour"):
            c = c.strip()
            # Normalize to lower-cased "#rrggbb"
            if c.startswith("#"):
                info["route_colour"] = c.lower()
            elif len(c) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in c):
                info["route_colour"] = "#" + c.lower()
        if "route_colour" in info:
            n_with_colour += 1
        if v := tags.get("name"):
            info["route_name"] = v
        # English name on the route relation, when OSM has it. Stored
        # alongside route_name so transit-layer.js can pick the right
        # one based on the user's active language; not used as a fallback
        # for route_name itself.
        if v := tags.get("name:en"):
            info["route_name_en"] = v
        if v := tags.get("ref"):
            info["route_ref"] = v
        if v := tags.get("operator"):
            info["route_operator"] = v
        if not info:
            continue
        for wid in way_ids:
            existing = way_map.get(wid)
            if existing is None:
                way_map[wid] = dict(info)
                continue
            # Prefer the relation that supplies a colour
            if "route_colour" in info and "route_colour" not in existing:
                way_map[wid] = dict(info)
            else:
                # Fill any blanks (e.g., name on a colour-less relation)
                for k, v in info.items():
                    existing.setdefault(k, v)
    print(f"  {n_rels} route relations parsed, {n_with_colour} with colour, "
          f"{len(way_map)} ways tagged")
    return way_map


def slim_way_props(props: dict, route_info: dict | None) -> dict | None:
    rw = props.get("railway")
    if rw not in KEEP_WAY_RAILWAY:
        return None
    if (props.get("usage") or "").lower() in SKIP_USAGE:
        return None
    if (props.get("service") or "").lower() in SKIP_SERVICE:
        return None
    out: dict[str, str] = {
        "kind": "line",
        "railway": rw,
        # `name` keeps the existing JP-with-EN-fallback semantics so any
        # consumer that only reads `name` keeps working. `name_en` is a
        # separate strict-English slot — empty when OSM has no name:en,
        # at which point the runtime falls back to `name`.
        "name": props.get("name") or props.get("name:en"),
        "name_en": props.get("name:en"),
        "operator": props.get("operator"),
        "ref": props.get("ref"),
    }
    # OSM way-level colour wins if present (rare); else inherit from route relation.
    if c := props.get("colour"):
        c = c.strip()
        if c.startswith("#"):
            out["colour"] = c.lower()
    if route_info:
        # Don't overwrite a way-level colour
        if "colour" not in out and "route_colour" in route_info:
            out["colour"] = route_info["route_colour"]
        # Prefer route_name over noisy per-segment name when route_name is cleaner.
        if route_info.get("route_name"):
            out["route_name"] = route_info["route_name"]
        if route_info.get("route_name_en"):
            out["route_name_en"] = route_info["route_name_en"]
        if route_info.get("route_ref"):
            out["route_ref"] = route_info["route_ref"]
        # If way has no operator, fall back to the route's
        if not out.get("operator") and route_info.get("route_operator"):
            out["operator"] = route_info["route_operator"]
    return {k: v for k, v in out.items() if v is not None}


def slim_node_props(props: dict) -> dict | None:
    rw = props.get("railway")
    if rw not in KEEP_NODE_RAILWAY:
        return None
    return {k: v for k, v in {
        "kind": "station",
        "railway": rw,
        # `name` keeps the JP-with-EN-fallback so the dedupe key works
        # whether or not the station has a Japanese tag. `name_en` is a
        # separate strict slot for runtime language switching.
        "name": props.get("name") or props.get("name:en"),
        "name_en": props.get("name:en"),
        "operator": props.get("operator"),
    }.items() if v is not None}


def round_coords(geom: dict, ndigits: int = 5) -> dict:
    t = geom["type"]
    if t == "Point":
        geom["coordinates"] = [round(c, ndigits) for c in geom["coordinates"]]
    elif t == "LineString":
        geom["coordinates"] = [[round(c, ndigits) for c in pt] for pt in geom["coordinates"]]
    return geom


def _perp_dist_sq(p, a, b) -> float:
    """Squared perpendicular distance from p to segment a-b, in degrees²."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    if t < 0.0:
        return (px - ax) ** 2 + (py - ay) ** 2
    if t > 1.0:
        return (px - bx) ** 2 + (py - by) ** 2
    proj_x = ax + t * dx
    proj_y = ay + t * dy
    return (px - proj_x) ** 2 + (py - proj_y) ** 2


def _approx_dist_sq_m(a, b) -> float:
    """Approximate squared distance in meters between two [lon, lat] points.
    Equirectangular approximation; lat→m uses 111000, lon→m uses 91000
    (correct at Tokyo's ~35°N). Plenty precise for sub-km thresholds."""
    dx = (a[0] - b[0]) * 91000.0
    dy = (a[1] - b[1]) * 111000.0
    return dx * dx + dy * dy


def dedupe_stations(stations: list) -> list:
    """Collapse same-name station nodes within STATION_MERGE_M into a single
    station at the cluster centroid. Operators (and railway types) are
    merged. Unnamed stations pass through untouched.

    Clustering is single-linkage by distance, scoped to within a name group —
    so e.g. all '市役所前' stations across Japan stay separate, but the five
    Shinjuku nodes that JR / Toei / Tokyo Metro each contribute collapse to one.
    """
    thresh_sq = STATION_MERGE_M ** 2
    by_name: dict[str, list] = {}
    unnamed = []
    for s in stations:
        nm = (s["properties"].get("name") or "").strip()
        if nm:
            by_name.setdefault(nm, []).append(s)
        else:
            unnamed.append(s)

    out = []
    n_collapsed = 0
    for nm, group in by_name.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        # Single-linkage clustering within this name group
        clusters: list[list] = []
        for s in group:
            coord = s["geometry"]["coordinates"]
            joined = False
            for cluster in clusters:
                for member in cluster:
                    if _approx_dist_sq_m(coord, member["geometry"]["coordinates"]) < thresh_sq:
                        cluster.append(s)
                        joined = True
                        break
                if joined:
                    break
            if not joined:
                clusters.append([s])
        for cluster in clusters:
            if len(cluster) == 1:
                out.append(cluster[0])
                continue
            n_collapsed += len(cluster) - 1
            xs = [m["geometry"]["coordinates"][0] for m in cluster]
            ys = [m["geometry"]["coordinates"][1] for m in cluster]
            cx = round(sum(xs) / len(xs), 5)
            cy = round(sum(ys) / len(ys), 5)
            operators = set()
            railways = set()
            name_en = None
            for m in cluster:
                if m["properties"].get("operator"):
                    operators.add(m["properties"]["operator"])
                railways.add(m["properties"].get("railway") or "station")
                # First non-empty name_en wins. Same Japanese name across
                # JR/Toei/Tokyo Metro contributions almost always points
                # to the same English name when any of them tag it, so a
                # vote-by-frequency would be overkill.
                if name_en is None and m["properties"].get("name_en"):
                    name_en = m["properties"]["name_en"]
            railway = "station" if "station" in railways else next(iter(railways))
            merged_props = {"kind": "station", "railway": railway, "name": nm}
            if name_en:
                merged_props["name_en"] = name_en
            if operators:
                merged_props["operator"] = "; ".join(sorted(operators))
            out.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [cx, cy]},
                "properties": merged_props,
            })

    out.extend(unnamed)
    print(f"  station dedup: {len(stations)} -> {len(out)} ({n_collapsed} collapsed)")
    return out


def simplify_dp(coords: list, tol_sq: float) -> list:
    """Iterative Douglas-Peucker on a LineString. Returns retained coords."""
    n = len(coords)
    if n < 3:
        return coords
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        max_d_sq = 0.0
        max_k = -1
        a = coords[i]
        b = coords[j]
        for k in range(i + 1, j):
            d_sq = _perp_dist_sq(coords[k], a, b)
            if d_sq > max_d_sq:
                max_d_sq = d_sq
                max_k = k
        if max_d_sq > tol_sq and max_k > 0:
            keep[max_k] = True
            stack.append((i, max_k))
            stack.append((max_k, j))
    return [coords[i] for i in range(n) if keep[i]]


def step_postprocess(way_route_map: dict[int, dict]) -> None:
    print(f"  reading {RAW_GEOJSON}")
    with RAW_GEOJSON.open(encoding="utf-8") as f:
        gj = json.load(f)
    n_in = len(gj["features"])
    line_feats = []
    station_feats = []
    colours_applied = 0
    coords_in = 0
    coords_out = 0
    for ftr in gj["features"]:
        gt = ftr["geometry"]["type"]
        props = ftr.get("properties") or {}
        fid = ftr.get("id") or props.get("@id") or ""
        if gt == "LineString":
            way_id = None
            if isinstance(fid, str) and fid.startswith("w"):
                try:
                    way_id = int(fid[1:])
                except ValueError:
                    pass
            route_info = way_route_map.get(way_id) if way_id else None
            slim = slim_way_props(props, route_info)
            if slim is None:
                continue
            if "colour" in slim and route_info and route_info.get("route_colour") == slim["colour"]:
                colours_applied += 1
            raw_coords = ftr["geometry"]["coordinates"]
            coords_in += len(raw_coords)
            simplified = simplify_dp(raw_coords, SIMPLIFY_TOL_SQ)
            coords_out += len(simplified)
            ftr["geometry"]["coordinates"] = simplified
            line_feats.append({
                "type": "Feature",
                "geometry": round_coords(ftr["geometry"]),
                "properties": slim,
            })
        elif gt == "Point":
            slim = slim_node_props(props)
            if slim is None:
                continue
            station_feats.append({
                "type": "Feature",
                "geometry": round_coords(ftr["geometry"]),
                "properties": slim,
            })

    station_feats = dedupe_stations(station_feats)
    out_feats = line_feats + station_feats

    n_lines = len(line_feats)
    n_pts = len(station_feats)
    pct = 100.0 * coords_out / coords_in if coords_in else 0
    print(f"  {n_in} in -> {len(out_feats)} out ({n_lines} lines, {n_pts} stations); "
          f"{colours_applied} lines inherited a route colour")
    print(f"  simplified coords: {coords_in:,} -> {coords_out:,} ({pct:.0f}%)")

    DOCS_TRANSIT.mkdir(parents=True, exist_ok=True)
    out = {"type": "FeatureCollection", "features": out_feats}
    FINAL_GEOJSON.write_text(
        json.dumps(out, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    size_mb = FINAL_GEOJSON.stat().st_size / 1024 / 1024
    print(f"  wrote {FINAL_GEOJSON} ({size_mb:.1f} MB)")

    # Second pass: amusement-park / 廃線 blocklist, 200m cross-name station
    # merge, line_count tagging for transfer-hub circles, is_longhaul split
    # for the 长途/市内 FAB pair. See transit_postprocess.py for the rules.
    print("  postprocessing (blocklist + cross-merge + line_count + longhaul)...")
    from transit_postprocess import postprocess as transit_postprocess
    transit_postprocess(FINAL_GEOJSON)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-intermediates", action="store_true",
                    help="don't delete japan-rail.geojson / japan-routes.opl after success")
    args = ap.parse_args()

    if not RAW_PBF.exists():
        sys.exit(f"missing {RAW_PBF}. Download japan-latest.osm.pbf from Geofabrik first.")

    step_filter_ways()
    step_filter_routes()
    step_dump_routes_opl()
    step_export()

    way_route_map = build_way_route_map(ROUTES_OPL)
    step_postprocess(way_route_map)

    if not args.keep_intermediates:
        for p in (RAW_GEOJSON, ROUTES_OPL):
            if p.exists():
                p.unlink()
                print(f"  cleaned up {p.name}")


if __name__ == "__main__":
    main()
