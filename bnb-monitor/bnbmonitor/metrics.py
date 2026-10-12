"""Metriche derivate (dinamica, weekend, 1 vs 2 notti) sopra le metriche giornaliere di mercato."""
from __future__ import annotations

import pandas as pd

KEYS = ["profile", "checkin", "seg_group"]
LEAD_POINTS = (60, 30, 14, 3)

SEG_GROUP_LABELS = {
    "appartamenti": "Appartamenti interi",
    "camere": "Camere (con hotel)",
    "camere_no_hotel": "Camere (senza hotel)",
}


def market(conn, radius_m: float | None = None, profile: str | None = None,
           seg_group: str | None = None) -> pd.DataFrame:
    """Metriche di mercato: dalla tabella precalcolata (centro) o calcolate al momento (raggio)."""
    if radius_m is None:
        sql = "SELECT * FROM metrics_market WHERE true"
        params: list = []
    else:
        sql = "SELECT * FROM market_metrics(%s, NULL, NULL) WHERE true"
        params = [radius_m]
    if profile:
        sql += " AND profile = %s"
        params.append(profile)
    if seg_group:
        sql += " AND seg_group = %s"
        params.append(seg_group)
    rows = conn.execute(sql, params).fetchall()
    return normalize(pd.DataFrame(rows))


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["observed_on", *KEYS, "n_active", "n_available", "availability_rate",
                                     "n_priced", "median_ppn", "p25_ppn", "p75_ppn", "own_available",
                                     "own_ppn", "price_index", "own_percentile"])
    df = df.copy()
    for c in ("observed_on", "checkin"):
        df[c] = pd.to_datetime(df[c])
    for c in ("availability_rate", "median_ppn", "p25_ppn", "p75_ppn", "own_ppn", "price_index",
              "own_percentile"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df


def latest(df: pd.DataFrame) -> pd.DataFrame:
    """Ultima rilevazione per ogni data di soggiorno (le date lontane non si controllano ogni notte)."""
    if df.empty:
        return df
    idx = df.groupby(KEYS)["observed_on"].idxmax()
    return df.loc[idx].sort_values(KEYS).reset_index(drop=True)


def with_7d_change(df: pd.DataFrame, tolerance_days: int = 3) -> pd.DataFrame:
    """Aggiunge pickup e variazioni a 7 giorni rispetto alla rilevazione di circa una settimana prima."""
    snap = latest(df)
    if snap.empty:
        return snap.assign(ref_observed_on=pd.NaT, pickup_7d=pd.NA, avail_change_pts=pd.NA,
                           median_change_pct=pd.NA)
    ref = df[["observed_on", *KEYS, "n_available", "availability_rate", "median_ppn"]].rename(
        columns={"observed_on": "ref_observed_on", "n_available": "ref_n_available",
                 "availability_rate": "ref_rate", "median_ppn": "ref_median"})
    m = snap.merge(ref, on=KEYS, how="left")
    age = (m["observed_on"] - m["ref_observed_on"]).dt.days
    m = m[(age >= 7) & (age <= 7 + tolerance_days)]
    m = m.sort_values("ref_observed_on").groupby(KEYS, as_index=False).last()
    out = snap.merge(m[[*KEYS, "ref_observed_on", "ref_n_available", "ref_rate", "ref_median"]],
                     on=KEYS, how="left")
    out["pickup_7d"] = out["ref_n_available"] - out["n_available"]
    out["avail_change_pts"] = (out["availability_rate"] - out["ref_rate"]) * 100
    out["median_change_pct"] = (out["median_ppn"] / out["ref_median"] - 1) * 100
    return out


def weekend_gap(snapshot: pd.DataFrame) -> pd.DataFrame:
    """Mediana dei prezzi per check-in venerdì/sabato contro gli altri giorni, per profilo e segmento."""
    if snapshot.empty:
        return pd.DataFrame(columns=["profile", "seg_group", "weekend", "weekday", "gap_pct"])
    s = snapshot.dropna(subset=["median_ppn"]).copy()
    s["is_weekend"] = s["checkin"].dt.weekday.isin([4, 5])
    g = s.groupby(["profile", "seg_group", "is_weekend"])["median_ppn"].median().unstack()
    g = g.rename(columns={True: "weekend", False: "weekday"}).reset_index()
    for c in ("weekend", "weekday"):
        if c not in g:
            g[c] = float("nan")
    g["gap_pct"] = (g["weekend"] / g["weekday"] - 1) * 100
    return g[["profile", "seg_group", "weekend", "weekday", "gap_pct"]]


def weekend_gap_history(df: pd.DataFrame) -> pd.DataFrame:
    """Andamento nel tempo di mediana weekend e infrasettimanale (per data di rilevazione)."""
    if df.empty:
        return pd.DataFrame(columns=["observed_on", "profile", "seg_group", "weekend", "weekday"])
    s = df.dropna(subset=["median_ppn"]).copy()
    s["kind"] = s["checkin"].dt.weekday.isin([4, 5]).map({True: "weekend", False: "weekday"})
    g = s.groupby(["observed_on", "profile", "seg_group", "kind"])["median_ppn"].median().unstack()
    return g.reset_index()


def lead_curve(df: pd.DataFrame, max_lead: int = 90) -> pd.DataFrame:
    """Prezzo mediano in funzione dei giorni all'arrivo (media sulle date di soggiorno osservate)."""
    if df.empty:
        return pd.DataFrame(columns=["profile", "seg_group", "lead_days", "median_ppn", "n_dates"])
    s = df.dropna(subset=["median_ppn"]).copy()
    s["lead_days"] = (s["checkin"] - s["observed_on"]).dt.days
    s = s[(s["lead_days"] >= 0) & (s["lead_days"] <= max_lead)]
    return (s.groupby(["profile", "seg_group", "lead_days"])
             .agg(median_ppn=("median_ppn", "median"), n_dates=("checkin", "nunique"))
             .reset_index())


def lead_points(curve: pd.DataFrame, tolerance: int = 2) -> pd.DataFrame:
    """Valori della curva a 60, 30, 14 e 3 giorni (con un minimo di tolleranza)."""
    rows = []
    for (profile, seg), g in curve.groupby(["profile", "seg_group"]):
        for p in LEAD_POINTS:
            near = g[(g["lead_days"] - p).abs() <= tolerance]
            if not near.empty:
                rows.append({"profile": profile, "seg_group": seg, "lead_days": p,
                             "median_ppn": float(near["median_ppn"].median()),
                             "n_dates": int(near["n_dates"].sum())})
    return pd.DataFrame(rows, columns=["profile", "seg_group", "lead_days", "median_ppn", "n_dates"])


def one_vs_two_nights(df: pd.DataFrame, long_profile: str, short_profile: str) -> pd.DataFrame:
    """Differenza del prezzo per notte tra soggiorno di 1 notte e soggiorno lungo."""
    cols = ["observed_on", "checkin", "seg_group", "median_ppn"]
    long_ = df[df["profile"] == long_profile][cols]
    short = df[df["profile"] == short_profile][cols]
    if long_.empty or short.empty:
        return pd.DataFrame(columns=["observed_on", "checkin", "seg_group", "ppn_long", "ppn_short", "gap_pct"])
    m = long_.merge(short, on=["observed_on", "checkin", "seg_group"], suffixes=("_long", "_short"))
    m = m.rename(columns={"median_ppn_long": "ppn_long", "median_ppn_short": "ppn_short"})
    m["gap_pct"] = (m["ppn_short"] / m["ppn_long"] - 1) * 100
    return m


def min_stay(conn, long_profile: str, short_profile: str, radius_m: float | None = None) -> pd.DataFrame:
    rows = conn.execute("SELECT * FROM min_stay_share(%s, %s, %s)",
                        (long_profile, short_profile, radius_m)).fetchall()
    df = pd.DataFrame(rows)
    if not df.empty:
        df["observed_on"] = pd.to_datetime(df["observed_on"])
        df["checkin"] = pd.to_datetime(df["checkin"])
        df["share"] = df["share"].astype(float)
    return df
