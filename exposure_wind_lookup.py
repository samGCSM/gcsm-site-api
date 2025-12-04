#!/usr/bin/env python3
"""
US Address → Exposure Category + Wind Speed (ASCE 7 screening)
-----------------------------------------------------------------
Now with:
  • Sector-by-sector upwind analysis (default 16 sectors)
  • Open-water (≥ 1 mile) fetch check for Great Lakes / bays / ocean
  • Corrected Exposure D inland extent: max(600 ft, 20× mean roof height) per ASCE 7-16/7-22 §26.7 and FBC adoption
  • CSV batch mode (--csv-in / --csv-out)
  • Wind speed fallback table (lightweight, expandable), plus best effort API lookup
  • Reverse geocoding (state + county) for smarter fallbacks
  • Land cover roughness proxies (OSM: wood/forest, farmland/meadow, residential/industrial) to refine B vs C
  • Minimal GUI (Tkinter) for quick one-off lookups

⚠️ LEGAL / ENGINEERING DISCLAIMER
- Screening-level tool only. NOT a sealed analysis. No warranties.
- Exposure per ASCE 7 requires sectoral upwind terrain assessment, fetch, obstructions, code edition, and judgment.
- This tool uses OpenStreetMap (OSM) and simple heuristics; OSM can be incomplete/incorrect.
- Wind speeds must be confirmed in the official ASCE 7 Hazard Tool (and FBC/local amendments) before use in design.
- Use professional judgment for topography (Kzt), importance (Risk), internal pressure, and component/cladding vs MWFRS.

Dependencies
- Python 3.9+
- pip install: requests shapely geographiclib pandas tkinterhtml (last is optional; Tkinter is stdlib)
  (Note: On some systems, Tkinter is a separate install.)

Usage (CLI)
- Single address:
    python exposure_wind_lookup.py "1200 SW 20th Ave, Cape Coral, FL 33991" --risk II --asce 7-16 --bldg-height 25
- JSON only:
    python exposure_wind_lookup.py "address" --json
- CSV batch:
    python exposure_wind_lookup.py --csv-in input.csv --csv-out results.csv --risk II --asce 7-16 --bldg-height 30
    # input.csv columns (header required): address
- GUI:
    python exposure_wind_lookup.py --gui
"""

import argparse
import csv
import json
import math
import sys
from typing import Dict, List, Optional, Tuple

import requests
from shapely.geometry import Point, Polygon, MultiPolygon, LineString
from shapely.ops import unary_union
from geographiclib.geodesic import Geodesic

try:
    import pandas as pd  # CSV batch, optional
except Exception:  # pragma: no cover
    pd = None

# GUI imports (lazy)
try:
    import tkinter as tk
    from tkinter import ttk
    HAVE_TK = True
except Exception:
    HAVE_TK = False

GEOD = Geodesic.WGS84
UA = {"User-Agent": "exposure-wind-estimator/3.0 (OSM/Overpass respectful access)"}

# ---------------------------------
# Geocoding (OSM Nominatim)
# ---------------------------------

def geocode(address: str) -> Tuple[float, float, str, Dict]:
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": address, "format": "json", "addressdetails": 1, "countrycodes": "us", "limit": 1}
    r = requests.get(url, params=params, headers=UA, timeout=25)
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError("Address not found")
    lat = float(data[0]["lat"])
    lon = float(data[0]["lon"])
    disp = data[0].get("display_name", address)
    addr_details = data[0].get("address", {})
    return lat, lon, disp, addr_details


def reverse_geocode(lat: float, lon: float) -> Dict:
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {"lat": lat, "lon": lon, "format": "json", "addressdetails": 1, "zoom": 10}
    r = requests.get(url, params=params, headers=UA, timeout=25)
    r.raise_for_status()
    return r.json().get("address", {})

# ---------------------------------
# Geometry helpers
# ---------------------------------

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    return GEOD.Inverse(lat1, lon1, lat2, lon2)["s12"]


def dest_point(lat, lon, azimuth_deg, distance_m) -> Tuple[float, float]:
    g = GEOD.Direct(lat, lon, azimuth_deg, distance_m)
    return g["lat2"], g["lon2"]

# ---------------------------------
# Overpass helpers
# ---------------------------------

def overpass(query: str) -> Dict:
    """
    Thin wrapper around Overpass API.

    IMPORTANT: This MUST NOT crash the whole app if Overpass times out or fails.
    On any error we return an empty structure so callers can gracefully fall back.
    """
    url = "https://overpass-api.de/api/interpreter"
    try:
        r = requests.post(url, data={"data": query}, headers=UA, timeout=60)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        # Log to console for debugging, but don't break FastAPI.
        print(f"[OVERPASS ERROR] {e}")
        # Overpass JSON normally has an "elements" key, so keep same shape.
        return {"elements": []}



def build_overpass_bbox(lat: float, lon: float, radius_m: float) -> Tuple[float, float, float, float]:
    dlat = radius_m / 111_000.0
    dlon = radius_m / (111_000.0 * math.cos(math.radians(lat)))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon

# ---------------------------------
# Coastline and water polygons (for Exposure D & fetch)
# ---------------------------------

def fetch_water_polys(lat: float, lon: float, radius_m: float = 80_000) -> Optional[MultiPolygon]:
    south, west, north, east = build_overpass_bbox(lat, lon, radius_m)
    q = f"""
    [out:json][timeout:60];
    (
      way["natural"="coastline"]({south},{west},{north},{east});
      way["natural"="water"]({south},{west},{north},{east});
      way["water"~"^(lake|reservoir|sea|bay)$"]({south},{west},{north},{east});
      relation["natural"="water"]({south},{west},{north},{east});
    );
    (._;>;);
    out body;
    """.strip()
    data = overpass(q)

    nodes = {el["id"]: (el["lat"], el["lon"]) for el in data.get("elements", []) if el.get("type") == "node"}
    ways = [el for el in data.get("elements", []) if el.get("type") == "way"]
    rels = [el for el in data.get("elements", []) if el.get("type") == "relation"]

    polys: List[Polygon] = []

    # Ways → polygons when closed
    for w in ways:
        nds = w.get("nodes", [])
        pts = [nodes[n] for n in nds if n in nodes]
        if len(pts) >= 4 and pts[0] == pts[-1]:
            try:
                poly = Polygon([(p[1], p[0]) for p in pts])  # lon, lat
                if poly.is_valid and not poly.is_empty:
                    polys.append(poly)
            except Exception:
                pass

    # Relations (multipolygons)
    for r in rels:
        if r.get("tags", {}).get("type") not in ("multipolygon", "boundary"):
            continue
        outers: List[LineString] = []
        inners: List[LineString] = []
        for m in r.get("members", []):
            if m.get("type") != "way":
                continue
            w = next((w for w in ways if w["id"] == m["ref"]), None)
            if not w:
                continue
            pts = [nodes[n] for n in w.get("nodes", []) if n in nodes]
            if len(pts) < 4 or pts[0] != pts[-1]:
                continue
            ring = LineString([(p[1], p[0]) for p in pts])
            if m.get("role") == "outer":
                outers.append(ring)
            elif m.get("role") == "inner":
                inners.append(ring)
        try:
            for outer in outers:
                poly = Polygon(outer)
                if inners:
                    poly = Polygon(outer, holes=[list(i.coords) for i in inners])
                if poly.is_valid and not poly.is_empty:
                    polys.append(poly)
        except Exception:
            pass

    if not polys:
        return None
    if len(polys) == 1:
        return MultiPolygon([polys[0]])
    return unary_union(polys)


def distance_to_water_boundary_m(lat: float, lon: float, water_polys: Optional[MultiPolygon]) -> Optional[float]:
    if not water_polys:
        return None
    pt = Point(lon, lat)
    # NOTE: This is an approximation; for small distances it's acceptable.
    return water_polys.boundary.distance(pt) * 111_000.0  # deg→m approx


def sector_over_water_fetch_m(
    lat: float,
    lon: float,
    azimuth_deg: float,
    max_check_m: float,
    water_polys: Optional[MultiPolygon],
) -> float:
    if not water_polys:
        return 0.0
    step_m = 200.0
    over_water = 0.0
    num_steps = int(max_check_m / step_m)
    for i in range(1, num_steps + 1):
        d = i * step_m
        lat2, lon2 = dest_point(lat, lon, azimuth_deg, d)
        if water_polys.contains(Point(lon2, lat2)):
            over_water = d
        else:
            break
    return over_water

# ---------------------------------
# Land cover / Roughness proxies (OSM)
# ---------------------------------

LANDCOVER_TAGS = [
    ("forest", {"natural": "wood"}),
    ("forest", {"landuse": "forest"}),
    ("farmland", {"landuse": "farmland"}),
    ("meadow", {"landuse": "meadow"}),
    ("residential", {"landuse": "residential"}),
    ("industrial", {"landuse": "industrial"}),
    ("commercial", {"landuse": "commercial"}),
]

ROUGHNESS_SCORE = {
    # Higher score → rougher terrain (tends toward Exposure B)
    "industrial": 1.0,
    "commercial": 0.95,
    "residential": 0.9,
    "forest": 0.8,
    "meadow": 0.5,
    "farmland": 0.4,
}

def landcover_mix(lat: float, lon: float, radius_m: float = 800) -> Dict[str, float]:
    south, west, north, east = build_overpass_bbox(lat, lon, radius_m)

    selectors = []
    for _, tag in LANDCOVER_TAGS:
        for k, v in tag.items():
            selectors.append(f'way["{k}"="{v}"]({south},{west},{north},{east});')

    selectors_str = "\n      ".join(selectors)

    q = f"""
    [out:json][timeout:60];
    (
      {selectors_str}
    );
    (._;>;);
    out body;
    """.strip()

    data = overpass(q)

    nodes = {el["id"]: (el["lat"], el["lon"]) for el in data.get("elements", []) if el.get("type") == "node"}
    ways = [el for el in data.get("elements", []) if el.get("type") == "way"]

    totals: Dict[str, float] = {k: 0.0 for k, _ in LANDCOVER_TAGS}
    for w in ways:
        tags = w.get("tags", {})
        label: Optional[str] = None
        for name, criteria in LANDCOVER_TAGS:
            if all(tags.get(k) == v for k, v in criteria.items()):
                label = name
                break
        if not label:
            continue
        nds = w.get("nodes", [])
        pts = [nodes[n] for n in nds if n in nodes]
        if len(pts) >= 4 and pts[0] == pts[-1]:
            try:
                poly = Polygon([(p[1], p[0]) for p in pts])
                if poly.is_valid:
                    # Rough conversion: deg² → m² → km²
                    area_km2 = poly.area * (111_000.0 ** 2) / 1_000_000.0
                    totals[label] += max(0.0, area_km2)
            except Exception:
                pass

    # Normalize to proportions
    total_area = sum(totals.values())
    if total_area > 0:
        for k in totals:
            totals[k] = totals[k] / total_area
    return totals


def roughness_index(mix: Dict[str, float]) -> float:
    if not mix:
        return 0.5
    return sum(ROUGHNESS_SCORE.get(k, 0.5) * p for k, p in mix.items())

# ---------------------------------
# Building density (Exposure B heuristic)
# ---------------------------------

def building_density_per_km2(lat: float, lon: float, radius_m: float = 800) -> float:
    south, west, north, east = build_overpass_bbox(lat, lon, radius_m)
    q = f"""
    [out:json][timeout:60];
    way["building"]({south},{west},{north},{east});
    out ids;
    """.strip()
    data = overpass(q)
    count = sum(1 for el in data.get("elements", []) if el.get("type") == "way")
    area_km2 = math.pi * (radius_m / 1000.0) ** 2
    return count / area_km2 if area_km2 > 0 else 0.0

# ---------------------------------
# Wind speed lookups
# ---------------------------------

def try_asce7_windspeed(lat: float, lon: float, risk_category: str, asce_edition: str) -> Optional[Dict]:
    """
    Best-effort unofficial API call to ASCE 7 hazard tool.
    If anything fails, returns None and caller must fall back.
    """
    try:
        url = "https://asce7hazardtool.online/api/windspeed"
        params = {
            "lat": lat,
            "lng": lon,
            "riskCategory": risk_category.upper().replace(" ", ""),
            "asceEdition": asce_edition,
        }
        r = requests.get(url, params=params, headers=UA, timeout=25)
        if r.status_code == 200:
            j = r.json()
            v = j.get("vult_mph") or j.get("Vult_mph") or j.get("Vult")
            if v:
                return {"Vult_mph": float(v), "source": url}
    except Exception:
        pass
    return None

# Lightweight fallback table — APPROXIMATE ONLY; VERIFY OFFICIALLY
WINDSPEED_FALLBACK: Dict[Tuple[str, str], float] = {
    ("FL", "miami-dade"): 170.0,
    ("FL", "monroe"): 180.0,
    ("FL", "broward"): 170.0,
}

# ---------------------------------
# Exposure decision (sectoral + roughness)
# ---------------------------------

def decide_exposure(
    lat: float,
    lon: float,
    building_height_ft: float,
    sectors: int = 16,
) -> Dict:
    water_polys = fetch_water_polys(lat, lon, radius_m=80_000)

    inland_limit_ft = max(600.0, 20.0 * building_height_ft)
    inland_limit_m = inland_limit_ft * 0.3048

    d_water_m = distance_to_water_boundary_m(lat, lon, water_polys)

    # 1) Sector over-water fetch
    sector_results: List[Dict] = []
    qualifies_D = False
    if water_polys is not None:
        for i in range(sectors):
            az = i * (360.0 / sectors)
            fetch_m = sector_over_water_fetch_m(
                lat,
                lon,
                azimuth_deg=az,
                max_check_m=10_000.0,
                water_polys=water_polys,
            )
            sector_results.append({"azimuth_deg": round(az, 1), "fetch_m": round(fetch_m, 1)})
            if fetch_m >= 1609.0:  # ≥ 1 mile over water
                if d_water_m is not None and d_water_m <= inland_limit_m:
                    qualifies_D = True

    # 2) Roughness proxies + building density for B vs C
    dens = building_density_per_km2(lat, lon, radius_m=800)
    mix = landcover_mix(lat, lon, radius_m=800)
    r_index = roughness_index(mix)

    notes: List[str] = []
    exposure: Optional[str] = None

    if qualifies_D:
        exposure = "D"
        notes.append("≥1 mile over-water fetch and within inland extent max(600 ft, 20×H) → Exposure D.")
    else:
        # Heuristic: if terrain is very rough OR dense buildings, pick B; else C
        # r_index scales ~0.4–1.0; choose threshold ~0.75 for B
        if dens >= 200 or r_index >= 0.75:
            exposure = "B"
            notes.append("Roughness/density threshold met (r_index≥0.75 or buildings≥200/km²) → Exposure B.")
        else:
            exposure = "C"
            notes.append("Open/suburban mix below thresholds → Exposure C.")

    # Diagnostics
    if d_water_m is not None:
        notes.append(
            f"Distance to mapped water boundary ≈ {d_water_m:.0f} m; inland limit ≈ {inland_limit_m:.0f} m "
            "(ASCE 7: max(600 ft, 20×H))."
        )
    notes.append(
        f"Building density (~800 m radius) ≈ {dens:.0f} buildings/km². Roughness index ≈ {r_index:.2f}."
    )
    if mix:
        notes.append("Land cover mix: " + ", ".join(f"{k}:{v:.0%}" for k, v in mix.items() if v > 0))

    return {
        "exposure_category": exposure,
        "distance_to_water_boundary_m": None if d_water_m is None else round(d_water_m, 1),
        "inland_limit_m": round(inland_limit_m, 1),
        "sector_results": sector_results,
        "building_density_per_km2": round(dens, 1),
        "roughness_index": round(r_index, 2),
        "landcover_mix": mix,
        "notes": notes,
    }

# ---------------------------------
# Utilities
# ---------------------------------

def parse_state_county(addr_details: Dict) -> Tuple[Optional[str], Optional[str]]:
    state = None
    county = None
    if not addr_details:
        return None, None
    state = addr_details.get("state") or addr_details.get("state_code")
    state_code = addr_details.get("state_code")
    if state_code and len(state_code) == 2:
        state = state_code
    else:
        # Minimal state name → abbrev map; extend as needed
        STATES = {
            "florida": "FL",
            "illinois": "IL",
            "alabama": "AL",
            "georgia": "GA",
            "texas": "TX",
            "california": "CA",
            "new york": "NY",
        }
        if state:
            state = STATES.get(state.lower(), None)
    county_name = addr_details.get("county") or addr_details.get("county_name")
    if county_name:
        county = county_name.lower().replace(" county", "")
    return state, county

# ---------------------------------
# Core analysis (callable from other code)
# ---------------------------------

def analyze_one_core(
    address: str,
    risk: str,
    asce: str,
    bldg_height_ft: float,
    sectors: int,
) -> Dict:
    """
    Core logic: returns a dict but does NOT print anything.
    Safe to call from FastAPI or other code.
    """
    lat, lon, disp, addr_details = geocode(address)
    # Reverse-geocode for robust county/state
    rev = reverse_geocode(lat, lon)
    # Prefer reverse values when available
    use_addr = rev if rev else addr_details

    wind = try_asce7_windspeed(lat, lon, risk, f"ASCE {asce}")
    if not wind:
        st, co = parse_state_county(use_addr)
        if st and co and (st, co) in WINDSPEED_FALLBACK:
            wind = {"Vult_mph": WINDSPEED_FALLBACK[(st, co)], "source": "fallback_table"}
        else:
            wind = {
                "Vult_mph": None,
                "source": "Use official ASCE 7 Hazard Tool (https://asce7hazardtool.online) and FBC/local amendments.",
            }

    exposure = decide_exposure(lat, lon, building_height_ft=bldg_height_ft, sectors=sectors)

    out = {
        "address": disp,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "risk_category": risk.upper(),
        "asce_edition": f"ASCE {asce}",
        "exposure_estimate": exposure,
        "wind_speed": wind,
        "disclaimer": (
            "Screening-only. Verify with official ASCE 7 Hazard Tool and governing code (FBC/IBC). "
            "Engineer-of-record responsible for final determinations (Exposure, Vult, Kzt, GCpi, etc.)."
        ),
    }
    return out


def analyze_one(
    address: str,
    risk: str,
    asce: str,
    bldg_height_ft: float,
    sectors: int,
    to_json: bool = False,
) -> Dict:
    """
    Backwards-compatible wrapper:
    - Calls analyze_one_core()
    - Optionally prints pretty text or JSON (for CLI).
    """
    out = analyze_one_core(address, risk, asce, bldg_height_ft, sectors)

    if to_json:
        print(json.dumps(out, indent=2))
    else:
        print("\n=== Address → Exposure + Wind (Screening) ===")
        print(f"Address: {out['address']} ({out['lat']}, {out['lon']})")
        print(f"ASCE: {out['asce_edition']}  Risk: {out['risk_category']}")

        e = out["exposure_estimate"]
        print(f"Exposure (estimated): {e['exposure_category']}")
        if e.get("distance_to_water_boundary_m") is not None:
            print(
                f"  ~Distance to water boundary: {e['distance_to_water_boundary_m']} m  "
                f"| Inland limit: {e['inland_limit_m']} m"
            )
        if e.get("notes"):
            for note in e["notes"]:
                print("  -", note)
        if e.get("sector_results"):
            print("  Sector over-water fetch (m):")
            for sr in e["sector_results"]:
                print(f"    az {sr['azimuth_deg']:>5}° → {sr['fetch_m']:>6.1f}")

        w = out["wind_speed"]
        print("Wind (Vult):", f"{w['Vult_mph']} mph" if w.get("Vult_mph") else "N/A (check ASCE 7 Hazard Tool)")
        print("Source:", w.get("source"))

        print("\nDISCLAIMER:")
        print(out["disclaimer"])
        print("\nTip: Run with --json for machine readable output.\n")

    return out

# ---------------------------------
# GUI (simple)
# ---------------------------------

def launch_gui():
    if not HAVE_TK:
        print("Tkinter not available on this system.")
        return

    root = tk.Tk()
    root.title("Exposure & Wind (ASCE 7 Screening)")

    frm = ttk.Frame(root, padding=10)
    frm.grid()

    tk.Label(frm, text="Address:").grid(column=0, row=0, sticky="e")
    addr_var = tk.StringVar()
    tk.Entry(frm, width=60, textvariable=addr_var).grid(column=1, row=0, columnspan=3, sticky="we")

    tk.Label(frm, text="Risk:").grid(column=0, row=1, sticky="e")
    risk_var = tk.StringVar(value="II")
    tk.Entry(frm, width=8, textvariable=risk_var).grid(column=1, row=1, sticky="w")

    tk.Label(frm, text="ASCE:").grid(column=2, row=1, sticky="e")
    asce_var = tk.StringVar(value="7-16")
    tk.Entry(frm, width=8, textvariable=asce_var).grid(column=3, row=1, sticky="w")

    tk.Label(frm, text="Mean Roof Height (ft):").grid(column=0, row=2, sticky="e")
    h_var = tk.DoubleVar(value=30.0)
    tk.Entry(frm, width=8, textvariable=h_var).grid(column=1, row=2, sticky="w")

    tk.Label(frm, text="# Sectors:").grid(column=2, row=2, sticky="e")
    sec_var = tk.IntVar(value=16)
    tk.Entry(frm, width=8, textvariable=sec_var).grid(column=3, row=2, sticky="w")

    output = tk.Text(frm, width=90, height=24)
    output.grid(column=0, row=4, columnspan=4, pady=(10, 0))

    def run_once_gui():
        output.delete("1.0", tk.END)
        a = addr_var.get().strip()
        if not a:
            output.insert(tk.END, "Enter an address.\n")
            return
        try:
            analyze_one(
                a,
                risk_var.get(),
                asce_var.get(),
                float(h_var.get()),
                int(sec_var.get()),
                to_json=False,
            )
            output.insert(tk.END, "DONE. See console output above for details.\n")
        except Exception as exc:
            output.insert(tk.END, f"ERROR: {exc}\n")

    ttk.Button(frm, text="Run", command=run_once_gui).grid(column=0, row=3, pady=6)

    root.mainloop()

# ---------------------------------
# Main
# ---------------------------------

def main():
    p = argparse.ArgumentParser(
        description="US Address → Exposure Category + Wind Speed (ASCE 7 screening)"
    )
    p.add_argument("address", nargs="?", help="US street address (ignored if --csv-in or --gui provided)")
    p.add_argument("--risk", default="II", help="ASCE 7 Risk Category (I, II, III, IV). Default: II")
    p.add_argument("--asce", default="7-16", help="ASCE 7 edition (7-10, 7-16, 7-22). Default: 7-16")
    p.add_argument(
        "--bldg-height",
        type=float,
        default=30.0,
        help="Mean roof height in feet. Default: 30 ft",
    )
    p.add_argument(
        "--sectors",
        type=int,
        default=16,
        help="Number of upwind sectors for fetch check. Default: 16",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Print machine readable JSON only",
    )
    p.add_argument(
        "--csv-in",
        help="CSV input with a column named 'address'",
    )
    p.add_argument(
        "--csv-out",
        help="CSV output path for results",
    )
    p.add_argument(
        "--gui",
        action="store_true",
        help="Launch minimal GUI",
    )
    args = p.parse_args()

    if args.gui:
        launch_gui()
        return

    # CSV batch mode
    if args.csv_in:
        rows: List[Dict] = []
        # Load addresses
        if pd is not None:
            df = pd.read_csv(args.csv_in)
            addrs = df["address"].astype(str).tolist()
        else:
            with open(args.csv_in, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                addrs = [r["address"] for r in reader]

        # Analyze each address
        for a in addrs:
            try:
                res = analyze_one_core(
                    a,
                    args.risk,
                    args.asce,
                    float(args.bldg_height),
                    int(args.sectors),
                )
                rows.append(
                    {
                        "address": res["address"],
                        "lat": res["lat"],
                        "lon": res["lon"],
                        "exposure": res["exposure_estimate"]["exposure_category"],
                        "Vult_mph": res["wind_speed"].get("Vult_mph"),
                        "asce": res["asce_edition"],
                        "risk": res["risk_category"],
                    }
                )
            except Exception as exc:
                rows.append({"address": a, "error": str(exc)})

        # Write CSV
        if args.csv_out:
            if pd is not None:
                pd.DataFrame(rows).to_csv(args.csv_out, index=False)
            else:
                with open(args.csv_out, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                    writer.writeheader()
                    writer.writerows(rows)
            print(f"Wrote results to {args.csv_out}")
        else:
            print(json.dumps(rows, indent=2))
        return

    # Single-address CLI mode
    if not args.address:
        print("ERROR: Provide an address or use --csv-in or --gui")
        sys.exit(2)

    analyze_one(
        args.address,
        args.risk,
        args.asce,
        float(args.bldg_height),
        int(args.sectors),
        to_json=args.json,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        sys.exit(1)
