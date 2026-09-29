"""
build_district_geojson.py — one-time build of Oregon legislative district
boundaries for the race map.

Downloads Census TIGER/Line 2024 shapefiles (public domain) for Oregon's
State House (SLDL, 60 districts) and State Senate (SLDU, 30 districts),
simplifies the geometry to web weight, and writes:

    docs/assets/or_house.geojson
    docs/assets/or_senate.geojson

Each feature carries properties {"name": "<district number>", "district": <int>}
— ECharts registerMap() joins series data on properties.name.

Run in CI (the district-geojson workflow) and commit the outputs; local
networks that corrupt large transfers should not run this.

Deps (installed ad hoc by the workflow): pyshp shapely
"""

from __future__ import annotations

import io
import json
import logging
import urllib.request
import zipfile
from pathlib import Path

import shapefile  # pyshp
from shapely.geometry import shape, mapping

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "assets"

SOURCES = {
    "or_house.geojson":  "https://www2.census.gov/geo/tiger/TIGER2024/SLDL/tl_2024_41_sldl.zip",
    "or_senate.geojson": "https://www2.census.gov/geo/tiger/TIGER2024/SLDU/tl_2024_41_sldu.zip",
}
SIMPLIFY_TOLERANCE = 0.002   # degrees ≈ 200 m; districts are large, this is generous
COORD_PRECISION = 4          # ~11 m — plenty for a choropleth


def round_coords(obj, nd=COORD_PRECISION):
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(v, nd) for v in obj]
        return [round_coords(v, nd) for v in obj]
    return obj


def build(out_name: str, url: str) -> None:
    log.info("Downloading %s …", url)
    with urllib.request.urlopen(url, timeout=120) as resp:
        zf = zipfile.ZipFile(io.BytesIO(resp.read()))
    shp_name = next(n for n in zf.namelist() if n.endswith(".shp"))
    base = shp_name[:-4]
    reader = shapefile.Reader(
        shp=io.BytesIO(zf.read(base + ".shp")),
        dbf=io.BytesIO(zf.read(base + ".dbf")),
        shx=io.BytesIO(zf.read(base + ".shx")),
    )
    fields = [f[0] for f in reader.fields[1:]]
    features = []
    for sr in reader.shapeRecords():
        rec = dict(zip(fields, sr.record))
        # SLDLST / SLDUST hold the zero-padded district number ("001"…"060");
        # ZZZ marks undefined/water areas.
        dist_raw = rec.get("SLDLST") or rec.get("SLDUST") or ""
        if not str(dist_raw).strip().isdigit():
            continue
        district = int(dist_raw)
        geom = shape(sr.shape.__geo_interface__).simplify(
            SIMPLIFY_TOLERANCE, preserve_topology=True)
        gj = mapping(geom)
        gj["coordinates"] = round_coords(gj["coordinates"])
        features.append({
            "type": "Feature",
            "properties": {"name": str(district), "district": district},
            "geometry": gj,
        })
    features.sort(key=lambda f: f["properties"]["district"])
    out = {"type": "FeatureCollection", "features": features}
    ASSETS.mkdir(parents=True, exist_ok=True)
    path = ASSETS / out_name
    path.write_text(json.dumps(out, separators=(",", ":")))
    log.info("Wrote %s: %d districts, %d KB",
             out_name, len(features), path.stat().st_size // 1024)


if __name__ == "__main__":
    for name, url in SOURCES.items():
        build(name, url)
