"""
Convert daily fire perimeter shapefiles to a single GeoPackage.

Input:  data/static/actual_perimeter/daily_fire_polygons/m3_polygons_YYYYMMDD/m3polygons.shp
Output: data/static/actual_perimeter/actual_perimeter.gpkg  (layer: fire_perimeters)
"""

import re
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_DIR = BASE_DIR / "data" / "static" / "actual_perimeter" / "daily_fire_polygons"
OUTPUT_FILE = BASE_DIR / "data" / "static" / "actual_perimeter" / "actual_perimeter.gpkg"
LAYER_NAME = "fire_perimeters"


def parse_date(folder_name: str) -> datetime | None:
    m = re.search(r"(\d{8})$", folder_name)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    return None


def load_all_shapefiles() -> gpd.GeoDataFrame:
    frames = []
    folders = sorted(INPUT_DIR.iterdir())
    total = len(folders)

    for i, folder in enumerate(folders, 1):
        shp = folder / "m3polygons.shp"
        if not shp.exists():
            print(f"  [skip] {folder.name} — no shp found")
            continue

        date = parse_date(folder.name)
        if date is None:
            print(f"  [skip] {folder.name} — cannot parse date")
            continue

        gdf = gpd.read_file(shp)
        gdf["date"] = date
        frames.append(gdf)

        if i % 20 == 0 or i == total:
            print(f"  {i}/{total} loaded")

    return pd.concat(frames, ignore_index=True) if frames else gpd.GeoDataFrame()


def main():
    print(f"Reading shapefiles from:\n  {INPUT_DIR}\n")
    combined = load_all_shapefiles()

    if combined.empty:
        print("No data loaded. Exiting.")
        return

    print(f"\nTotal features: {len(combined)}")
    print(f"Date range: {combined['date'].min()} → {combined['date'].max()}")
    print(f"CRS: {combined.crs}")

    print(f"\nWriting GPKG to:\n  {OUTPUT_FILE}")
    combined.to_file(OUTPUT_FILE, layer=LAYER_NAME, driver="GPKG")
    print("Done.")


if __name__ == "__main__":
    main()
