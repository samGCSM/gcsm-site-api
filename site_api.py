# site_api.py
"""
FastAPI layer that ties together:
- exposure_wind_lookup.analyze_one  (exposure + Vult from ASCE hazard tool)
- fpa_fastener_gui.run_once         (FPA zone patterns)
- optional Google Street View URL

Run:
    uvicorn site_api:app --reload --port 8000
"""

import os
import urllib.parse
from typing import Optional, Any, Dict

from fastapi import FastAPI
from pydantic import BaseModel

# Import your existing logic
from exposure_wind_lookup import analyze_one
from fpa_fastener_gui import run_once as fpa_run_once
from site_specific_desktop_app import round_up  # for Z1/Z2/Z3 if we want

# If you have a Google Street View API key, set this as an env var
GOOGLE_STREETVIEW_KEY = os.getenv("GOOGLE_STREETVIEW_KEY")

# Your existing FPA directory
DEFAULT_FPA_DIR = r"Q:\Engineering\All current FPAs"

app = FastAPI(title="GCSM Site Analyzer API")


# ---------- Request/Response models ----------

class SiteRequest(BaseModel):
    address: str

    # Exposure / wind
    risk_category: str = "II"        # I, II, III, IV
    asce_edition: str = "7-16"       # "7-10", "7-16", "7-22"
    mean_roof_height_ft: float = 25.0
    sectors: int = 16

    # FPA inputs
    panel: str = "GulfSeam"
    exposure_override: Optional[str] = None  # "B", "C", "D" to override auto exposure
    fpa_height_ft: int = 30                 # must be one of [20,25,30,40,50,60]
    wind_col_mph: int = 150                 # one of [120,130,140,...,200]

    # For now we’ll skip automatic PDF; we can add it later
    # generate_pdf: bool = False


class SiteResponse(BaseModel):
    address: str
    lat: float
    lon: float

    streetview_url: Optional[str]

    exposure_category: Optional[str]
    exposure_details: Dict[str, Any]

    wind_speed_vult_mph: Optional[float]
    wind_source: Optional[str]

    fpa_zone_results: Dict[str, Any]


# ---------- Helpers ----------

def build_streetview_url(address: str) -> Optional[str]:
    """
    Returns a Google Street View static image URL, or None if no API key set.
    """
    if not GOOGLE_STREETVIEW_KEY:
        return None
    loc = urllib.parse.quote_plus(address)
    return (
        "https://maps.googleapis.com/maps/api/streetview"
        f"?size=640x400&location={loc}&key={GOOGLE_STREETVIEW_KEY}"
    )


# ---------- Routes ----------

@app.get("/")
def root():
    return {"status": "ok", "message": "GCSM Site Analyzer API running"}


@app.post("/analyze", response_model=SiteResponse)
def analyze_site(req: SiteRequest):
    """
    Main endpoint:
    1) Uses exposure_wind_lookup.analyze_one() to get exposure + wind.
    2) Uses fpa_fastener_gui.run_once() to get FPA zone patterns.
    3) Returns JSON with everything plus a Street View URL.
    """

    # 1) Exposure + wind
    ew = analyze_one(
        req.address,
        req.risk_category,
        req.asce_edition,
        float(req.mean_roof_height_ft),
        int(req.sectors),
        to_json=False,  # don't print JSON, but it will still print text; that's fine for now
    )

    exposure_from_tool = ew["exposure_estimate"]["exposure_category"]
    exposure_final = req.exposure_override or exposure_from_tool

    wind_speed_vult = ew["wind_speed"].get("Vult_mph")
    wind_source = ew["wind_speed"].get("source")

    # 2) FPA fastener selection
    fpa_result = fpa_run_once(
        panel=req.panel,
        address=req.address,
        exposure=exposure_final,
        height=int(req.fpa_height_ft),
        wind_col=int(req.wind_col_mph),
        fpa_dir=DEFAULT_FPA_DIR,
    )

    # For convenience, if status is ok, just return the zone_results dict; otherwise the whole error.
    if fpa_result.get("status") == "ok":
        fpa_zone_results = fpa_result.get("zone_results", {})
    else:
        fpa_zone_results = {"error": fpa_result}

    # 3) Build response
    return SiteResponse(
        address=ew["address"],
        lat=ew["lat"],
        lon=ew["lon"],
        streetview_url=build_streetview_url(ew["address"]),
        exposure_category=exposure_final,
        exposure_details=ew["exposure_estimate"],
        wind_speed_vult_mph=wind_speed_vult,
        wind_source=wind_source,
        fpa_zone_results=fpa_zone_results,
    )
