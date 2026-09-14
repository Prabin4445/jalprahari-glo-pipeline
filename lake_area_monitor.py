#!/usr/bin/env python3
"""
JalPrahari GLO lake-area monitor.

Measures the current surface area of each GLOF watch lake from fresh
Sentinel-2 optical imagery (Sentinel-1 SAR fallback when clouds block the
optical view), compares it against the GLO Sentinel-2 inventory 2024
baseline, and writes a JSON feed the JalPrahari app consumes.

Runs headless: Google Earth Engine service-account credentials come from
the EE_SERVICE_ACCOUNT / EE_KEY_PATH environment variables (GitHub Actions
secrets). Locally, plain `earthengine authenticate` works too.

Output: data/glo-lakes-latest.json
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import ee

# --------------------------------------------------------------------------
# Watch lakes. Coordinates are the real GLO_ID centroids from the
# GLO Sentinel-2 inventory 2017-2024 (Zenodo 17802334, CC BY 4.0).
# baseline_km2 = 2024 lake area from that same dataset.
# --------------------------------------------------------------------------
LAKES = [
    {
        "glo_id": "GLO_86.56368_27.83346",
        "name_en": "Thame Glacial Lake (Tomutse)",
        "lon": 86.56368, "lat": 27.83346,
        "baseline_km2": 0.1038,
    },
    {
        "glo_id": "GLO_86.47904_27.85858",
        "name_en": "Tsho Rolpa",
        "lon": 86.47904, "lat": 27.85858,
        "baseline_km2": 1.6704,
    },
    {
        "glo_id": "GLO_86.92845_27.89838",
        "name_en": "Imja Tsho",
        "lon": 86.92845, "lat": 27.89838,
        "baseline_km2": 1.8539,
    },
    {
        "glo_id": "GLO_87.08864_27.79792",
        "name_en": "Lower Barun (Barun Tsho)",
        "lon": 87.08864, "lat": 27.79792,
        "baseline_km2": 2.4301,
    },
    {
        "glo_id": "GLO_86.06569_28.06715",
        "name_en": "Cirenmaco / Poiqu Lake",
        "lon": 86.06569, "lat": 28.06715,
        "baseline_km2": 0.3042,
    },
]

BUFFER_M = 2500          # search radius around the lake centroid
WINDOW_DAYS = 45         # look back this far for a clear observation
S2_MAX_CLOUD_PCT = 30    # skip scenes cloudier than this
NDWI_THRESHOLD = 0.15    # water if NDWI > threshold
S1_WATER_VV_DB = -16.0   # water if Sentinel-1 VV backscatter below this (dB)

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "glo-lakes-latest.json")


def init_earth_engine():
    email = os.environ.get("EE_SERVICE_ACCOUNT")
    key_path = os.environ.get("EE_KEY_PATH", "ee-key.json")
    if email and os.path.exists(key_path):
        creds = ee.ServiceAccountCredentials(email, key_path)
        ee.Initialize(creds)
        print(f"Earth Engine: service account {email}")
    else:
        ee.Initialize()  # local `earthengine authenticate` credentials
        print("Earth Engine: default credentials")


def _date_str(d):
    """Earth Engine filterDate wants 'YYYY-MM-DD' text; accept datetimes too."""
    return d if isinstance(d, str) else d.strftime("%Y-%m-%d")


def s2_clear_image(buf, start, end):
    """Least-cloudy Sentinel-2 surface-reflectance image in the window."""
    col = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
           .filterBounds(buf)
           .filterDate(_date_str(start), _date_str(end))
           .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", S2_MAX_CLOUD_PCT))
           .sort("CLOUDY_PIXEL_PERCENTAGE"))
    n = col.size().getInfo()
    if n == 0:
        return None
    return ee.Image(col.first())


def mask_s2_clouds(img):
    """Keep vegetation/soil/water/unclassified/snow-ice; drop cloud/shadow."""
    scl = img.select("SCL")
    keep = scl.eq(4).Or(scl.eq(5)).Or(scl.eq(6)).Or(scl.eq(7)).Or(scl.eq(11))
    return img.updateMask(keep)


def _plausible_water_mask():
    """JRC Global Surface Water max-extent mask (1984-2021), dilated ~60 m.

    Stops wet monsoon ground / moist glacier ice being counted as lake,
    while leaving room for real lake expansion beyond the 2021 extent.
    """
    gsw = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("max_extent")
    return gsw.eq(1).focal_max(radius=60, units="meters")


def s2_water_area_km2(img, buf):
    img = mask_s2_clouds(img)
    ndwi = img.normalizedDifference(["B3", "B8"]).rename("NDWI")
    water = ndwi.gt(NDWI_THRESHOLD).And(_plausible_water_mask()).rename("NDWI")
    area = water.multiply(ee.Image.pixelArea()).divide(1e6)
    total = area.reduceRegion(
        reducer=ee.Reducer.sum(), geometry=buf, scale=10,
        maxPixels=1e8).get("NDWI")
    return total.getInfo()


def s1_water_area_km2(buf, start, end):
    """Sentinel-1 SAR fallback: sees through clouds and darkness."""
    col = (ee.ImageCollection("COPERNICUS/S1_GRD")
           .filterBounds(buf)
           .filterDate(_date_str(start), _date_str(end))
           .filter(ee.Filter.eq("instrumentMode", "IW"))
           .filter(ee.Filter.listContains(
               "transmitterReceiverPolarisation", "VV")))
    n = col.size().getInfo()
    if n == 0:
        return None
    vv = col.select("VV").median()
    water = vv.lt(S1_WATER_VV_DB).And(_plausible_water_mask()).rename("VV")
    area = water.multiply(ee.Image.pixelArea()).divide(1e6)
    total = area.reduceRegion(
        reducer=ee.Reducer.sum(), geometry=buf, scale=10,
        maxPixels=1e8).get("VV")
    return total.getInfo()


def indicator_for(change_pct):
    """App monitoring indicator only — never presented as an official warning."""
    a = abs(change_pct)
    if a < 2:
        return "stable", "🟢"
    if a < 5:
        return ("increasing" if change_pct > 0 else "shrinking"), "🟡"
    if a < 10:
        return ("rapid" if change_pct > 0 else "shrinking"), "🟠"
    return ("concern" if change_pct > 0 else "shrinking"), "🔴"


def measure_lake(lake, start, end):
    pt = ee.Geometry.Point([lake["lon"], lake["lat"]])
    buf = pt.buffer(BUFFER_M)
    result = {
        "glo_id": lake["glo_id"],
        "name_en": lake["name_en"],
        "baseline_km2": lake["baseline_km2"],
        "current_km2": None,
        "observed_at": None,
        "sensor": None,
        "cloud_pct": None,
        "change_km2": None,
        "change_pct": None,
        "indicator": "no_observation",
        "note": "no clear satellite observation in window",
    }

    # 1) Sentinel-2 optical (preferred: 10 m, true water index)
    try:
        img = s2_clear_image(buf, start, end)
        if img is not None:
            area = s2_water_area_km2(img, buf)
            if area is not None:
                ts = img.get("system:time_start").getInfo()
                cloud = img.get("CLOUDY_PIXEL_PERCENTAGE").getInfo()
                result.update({
                    "current_km2": round(float(area), 4),
                    "observed_at": datetime.fromtimestamp(
                        ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                    "sensor": "Sentinel-2",
                    "cloud_pct": round(float(cloud), 1),
                })
    except Exception as e:  # noqa: BLE001 - one lake must not kill the run
        print(f"  S2 failed for {lake['name_en']}: {e}", file=sys.stderr)

    # 2) Sentinel-1 SAR fallback (cloud-penetrating radar)
    if result["current_km2"] is None:
        try:
            area = s1_water_area_km2(buf, start, end)
            if area is not None:
                result.update({
                    "current_km2": round(float(area), 4),
                    "observed_at": end.strftime("%Y-%m-%d"),
                    "sensor": "Sentinel-1 (SAR, cloud-penetrating)",
                    "note": "radar fallback; optical blocked by cloud",
                })
        except Exception as e:  # noqa: BLE001
            print(f"  S1 failed for {lake['name_en']}: {e}", file=sys.stderr)

    if result["current_km2"] is not None:
        base = lake["baseline_km2"]
        change = result["current_km2"] - base
        pct = (change / base * 100.0) if base > 0 else 0.0
        ind, emoji = indicator_for(pct)
        result.update({
            "change_km2": round(change, 4),
            "change_pct": round(pct, 2),
            "indicator": ind,
            "emoji": emoji,
            "note": ("app monitoring indicator vs 2024 GLO baseline; "
                     "not an official GLOF warning"),
        })
    return result


def main():
    init_earth_engine()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=WINDOW_DAYS)
    start_s, end_s = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    print(f"Window: {start_s} .. {end_s}")

    lakes = []
    for lake in LAKES:
        print(f"Measuring {lake['name_en']} ...")
        r = measure_lake(lake, start, end)
        print(f"  -> {r['current_km2']} km2 ({r['sensor']}) "
              f"change {r['change_pct']}% [{r['indicator']}]")
        lakes.append(r)

    payload = {
        "updated_at": end.isoformat(),
        "source": "google-earth-engine",
        "sensors": "Sentinel-2 optical, Sentinel-1 SAR fallback",
        "baseline": "GLO Sentinel-2 inventory 2017-2024 (2024 area)",
        "window_days": WINDOW_DAYS,
        "disclaimer": ("App monitoring indicators only. Not official GLOF "
                       "warnings; follow DHM / NDRRMA advisories."),
        "lakes": lakes,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
