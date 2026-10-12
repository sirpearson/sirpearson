"""Geometria: poligono del centro, distanze in linea d'aria."""
from __future__ import annotations

import json
import math
from pathlib import Path

EARTH_RADIUS_M = 6_371_008.8


def load_polygon(path: Path) -> list[tuple[float, float]]:
    """Legge il primo poligono di un file GeoJSON; restituisce l'anello esterno come (lon, lat)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("type") == "FeatureCollection":
        geoms = [f["geometry"] for f in data["features"]]
    elif data.get("type") == "Feature":
        geoms = [data["geometry"]]
    else:
        geoms = [data]
    for g in geoms:
        if g["type"] == "Polygon":
            ring = g["coordinates"][0]
        elif g["type"] == "MultiPolygon":
            ring = g["coordinates"][0][0]
        else:
            continue
        pts = [(float(x), float(y)) for x, y, *_ in ring]
        if pts[0] == pts[-1]:
            pts = pts[:-1]
        if len(pts) < 3:
            raise ValueError(f"{path}: poligono con meno di 3 vertici")
        return pts
    raise ValueError(f"{path}: nessun poligono trovato")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def point_in_polygon(lat: float, lon: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray casting su coordinate (lon, lat); adeguato per aree piccole come un centro città."""
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def centroid(polygon: list[tuple[float, float]]) -> tuple[float, float]:
    """Baricentro dell'area del poligono, come (lat, lon)."""
    a = cx = cy = 0.0
    n = len(polygon)
    for i in range(n):
        x0, y0 = polygon[i]
        x1, y1 = polygon[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-15:
        xs, ys = zip(*polygon)
        return sum(ys) / n, sum(xs) / n
    a *= 0.5
    return cy / (6 * a), cx / (6 * a)


def max_vertex_distance(point: tuple[float, float], polygon: list[tuple[float, float]]) -> float:
    lat, lon = point
    return max(haversine_m(lat, lon, y, x) for x, y in polygon)


def circle(lat: float, lon: float, radius_m: float, n: int = 64) -> list[tuple[float, float]]:
    """Punti (lat, lon) di un cerchio, per disegnare il raggio sulla mappa."""
    pts = []
    for k in range(n + 1):
        t = 2 * math.pi * k / n
        dlat = (radius_m * math.cos(t)) / EARTH_RADIUS_M
        dlon = (radius_m * math.sin(t)) / (EARTH_RADIUS_M * math.cos(math.radians(lat)))
        pts.append((lat + math.degrees(dlat), lon + math.degrees(dlon)))
    return pts
