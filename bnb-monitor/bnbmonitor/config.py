"""Caricamento della configurazione (config/config.yaml + variabili d'ambiente)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import geo

DEFAULT_CONFIG_DIR = Path(os.environ.get("BNB_CONFIG_DIR", Path(__file__).resolve().parent.parent / "config"))
DEFAULT_DATA_DIR = Path(os.environ.get("BNB_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))


@dataclass(frozen=True)
class Profile:
    key: str
    label: str
    nights: int
    adults: int
    horizon_days: int
    near_days: int
    far_every_days: int


@dataclass
class Config:
    raw: dict[str, Any]
    config_dir: Path
    data_dir: Path
    profiles: dict[str, Profile]
    polygon: list[tuple[float, float]]  # (lon, lat)
    search_point: tuple[float, float]    # (lat, lon)
    stop_radius_m: float
    database_url: str
    telegram_token: str | None
    telegram_chat_id: str | None
    own: dict[str, Any] = field(default_factory=dict)

    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name) or {}

    @property
    def own_point(self) -> tuple[float, float]:
        return float(self.own["lat"]), float(self.own["lon"])

    @property
    def debug_dir(self) -> Path:
        return self.data_dir / "debug"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    user = os.environ.get("POSTGRES_USER", "bnb")
    pwd = os.environ.get("POSTGRES_PASSWORD", "bnb")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "bnb")
    return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"


def load(config_dir: Path | str | None = None, data_dir: Path | str | None = None) -> Config:
    config_dir = Path(config_dir or DEFAULT_CONFIG_DIR)
    data_dir = Path(data_dir or DEFAULT_DATA_DIR)
    raw = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8")) or {}

    profiles = {
        key: Profile(
            key=key,
            label=p.get("label", key),
            nights=int(p["nights"]),
            adults=int(p["adults"]),
            horizon_days=int(p["horizon_days"]),
            near_days=int(p.get("near_days", 30)),
            far_every_days=int(p.get("far_every_days", 3)),
        )
        for key, p in (raw.get("profiles") or {}).items()
    }
    if not profiles:
        raise ValueError("config.yaml: nessun profilo di ricerca definito")

    g = raw.get("geo") or {}
    polygon = geo.load_polygon(config_dir / g.get("polygon_file", "centro.geojson"))
    if g.get("search_lat") is not None and g.get("search_lon") is not None:
        search_point = (float(g["search_lat"]), float(g["search_lon"]))
    else:
        search_point = geo.centroid(polygon)
    stop_radius = g.get("stop_radius_m")
    if stop_radius is None:
        stop_radius = geo.max_vertex_distance(search_point, polygon) + float(g.get("stop_margin_m", 250))

    return Config(
        raw=raw,
        config_dir=config_dir,
        data_dir=data_dir,
        profiles=profiles,
        polygon=polygon,
        search_point=search_point,
        stop_radius_m=float(stop_radius),
        database_url=database_url(),
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
        own=dict(raw.get("own_property") or {}),
    )
