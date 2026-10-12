"""Dashboard Streamlit del Monitor prezzi BnB Trieste."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bnbmonitor import config as bconfig, db, geo, metrics  # noqa: E402

st.set_page_config(page_title="Monitor BnB Trieste", page_icon="🏠", layout="wide")

# Palette (riferimento dataviz): categoriali in ordine fisso, sequenziale blu.
SERIES = {"appartamenti": "#2a78d6", "camere": "#eb6834", "camere_no_hotel": "#1baf7a"}
SEQ_BLUE = [[0, "#cde2fb"], [0.25, "#86b6ef"], [0.5, "#3987e5"], [0.75, "#1c5cab"], [1, "#0d366b"]]
OWN_COLOR = "#e34948"
MUTED = "#8a8985"
WEEKDAYS = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
PLOT_LAYOUT = dict(margin=dict(l=10, r=10, t=40, b=20), hovermode="closest",
                   font=dict(size=13), legend=dict(orientation="h", yanchor="top", y=-0.22, x=0))


# --- dati -------------------------------------------------------------------------------

@st.cache_resource
def get_cfg():
    return bconfig.load()


@st.cache_resource
def get_conn():
    conn = db.connect(get_cfg().database_url, retries=3)
    conn.autocommit = True
    return conn


def q(sql: str, params=None) -> pd.DataFrame:
    try:
        rows = get_conn().execute(sql, params or []).fetchall()
    except Exception:  # noqa: BLE001 - connessione caduta: si riapre
        get_conn.clear()
        rows = get_conn().execute(sql, params or []).fetchall()
    return pd.DataFrame(rows)


@st.cache_data(ttl=600)
def load_market(radius_m: float | None) -> pd.DataFrame:
    try:
        return metrics.market(get_conn(), radius_m)
    except Exception:  # noqa: BLE001
        get_conn.clear()
        return metrics.market(get_conn(), radius_m)


@st.cache_data(ttl=600)
def load_properties_for(profile: str, checkin: date, seg_group: str, radius_m: float | None) -> pd.DataFrame:
    df = q(
        """SELECT p.booking_id, p.name, p.segment, p.lat, p.lon, p.distance_m, p.rating, p.review_count,
                  p.is_own, o.observed_on, o.available, o.price_per_night::float AS ppn,
                  o.price_total::float AS price_total, o.breakfast, o.free_cancellation
           FROM properties p
           LEFT JOIN LATERAL (
               SELECT * FROM observations o
               WHERE o.booking_id = p.booking_id AND o.profile = %(profile)s AND o.checkin = %(checkin)s
               ORDER BY o.observed_on DESC, o.run_id DESC LIMIT 1) o ON true
           WHERE p.is_own
              OR (p.in_polygon AND p.active
                  AND p.segment IN (SELECT segment FROM segment_groups WHERE seg_group = %(seg)s)
                  AND (%(radius)s::float IS NULL OR p.distance_m <= %(radius)s::float))
           ORDER BY p.distance_m NULLS LAST""",
        {"profile": profile, "checkin": checkin, "seg": seg_group, "radius": radius_m})
    if not df.empty:
        df["rating"] = pd.to_numeric(df["rating"], errors="coerce").astype(float)
    return df


@st.cache_data(ttl=600)
def load_min_stay(long_p: str, short_p: str, radius_m: float | None) -> pd.DataFrame:
    return metrics.min_stay(get_conn(), long_p, short_p, radius_m)


def show(fig) -> None:
    xs = [t.x for t in fig.data if getattr(t, "x", None) is not None and len(t.x)]
    if xs and pd.api.types.is_datetime64_any_dtype(pd.Series(xs[0])):
        fig.update_xaxes(tickformat="%d/%m")
    st.plotly_chart(fig, use_container_width=True, theme="streamlit")


def euro(v) -> str:
    return "—" if v is None or pd.isna(v) else f"€ {v:,.0f}".replace(",", ".")


def pct(v, digits: int = 0) -> str:
    return "—" if v is None or pd.isna(v) else f"{v:.{digits}%}"


# --- filtri globali ---------------------------------------------------------------------------

cfg = get_cfg()
try:
    get_conn()
except Exception as exc:  # noqa: BLE001
    st.error(f"Database non raggiungibile: {exc}")
    st.stop()

st.sidebar.title("🏠 Monitor BnB Trieste")
page = st.sidebar.radio("Pagina", ["Calendario", "Dettaglio data", "Mappa", "Tendenze", "Stato del sistema"])
st.sidebar.divider()
profile = st.sidebar.radio("Profilo", list(cfg.profiles), format_func=lambda k: cfg.profiles[k].label)
seg_choice = st.sidebar.radio("Segmento", ["appartamenti", "camere"],
                              format_func=lambda s: {"appartamenti": "Appartamenti interi",
                                                     "camere": "Camere per 2"}[s])
seg_group = seg_choice
if seg_choice == "camere":
    seg_group = "camere" if st.sidebar.checkbox("Includi gli hotel", value=True) else "camere_no_hotel"
max_r = int(round(cfg.stop_radius_m / 50) * 50)
radius_val = st.sidebar.slider("Raggio da Via Economo (m)", 100, max_r, max_r, step=50,
                               help="Al massimo = tutta la zona centro")
radius_m = None if radius_val >= max_r else float(radius_val)
st.sidebar.caption("Zona: " + ("tutto il centro" if radius_m is None else f"entro {radius_val} m dal BnB"))

market_all = load_market(radius_m)
mk = market_all[(market_all["profile"] == profile) & (market_all["seg_group"] == seg_group)] \
    if not market_all.empty else market_all
snap = metrics.latest(mk)
today = pd.Timestamp(date.today())
if not snap.empty:
    snap = snap[snap["checkin"] >= today]

seg_label = metrics.SEG_GROUP_LABELS[seg_group]


def no_data():
    st.info("Ancora nessun dato valido per questi filtri. Le metriche compaiono dopo la prima "
            "esecuzione notturna valida.")


# --- pagine -------------------------------------------------------------------------------------

def page_calendar():
    st.header("Calendario delle prossime 90 notti")
    st.caption(f"{cfg.profiles[profile].label} · {seg_label} · ultima rilevazione per ogni data")
    if snap.empty:
        return no_data()
    s = snap[snap["checkin"] < today + timedelta(days=90)].copy()
    s["occupancy"] = 1 - s["availability_rate"]

    next30 = s[s["checkin"] < today + timedelta(days=30)]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Disponibilità media (30 notti)", pct(next30["availability_rate"].mean()))
    c2.metric("Mediana per notte (30 notti)", euro(next30["median_ppn"].median()))
    c3.metric("Indice di prezzo BnB", "—" if next30["price_index"].isna().all()
              else f"{next30['price_index'].median():.2f}",
              help="Prezzo del BnB / mediana del gruppo; 1,00 = in linea con il mercato")
    c4.metric("Notti del BnB già vendute*", f"{int((next30['own_available'] == False).sum())}/{len(next30)}",  # noqa: E712
              help="*Notti in cui il BnB non compare disponibile su Booking")

    # Griglia settimane x giorni.
    s["week_start"] = s["checkin"] - pd.to_timedelta(s["checkin"].dt.weekday, unit="D")
    s["wd"] = s["checkin"].dt.weekday
    weeks = sorted(s["week_start"].unique())
    z, text, hover = [], [], []
    for w in weeks:
        zr, tr, hr = [], [], []
        for wd in range(7):
            r = s[(s["week_start"] == w) & (s["wd"] == wd)]
            if r.empty:
                zr.append(None)
                tr.append("")
                hr.append("")
                continue
            r = r.iloc[0]
            zr.append(r["occupancy"])
            own = euro(r["own_ppn"]) if pd.notna(r["own_ppn"]) else ("venduta" if r["own_available"] is False else "—")
            tr.append(f"{r['checkin']:%d/%m}<br>BnB {own}<br>med {euro(r['median_ppn'])}")
            hr.append(
                f"<b>{WEEKDAYS[wd]} {r['checkin']:%d/%m/%Y}</b><br>"
                f"Disponibili: {int(r['n_available'])}/{int(r['n_active'])} ({pct(r['availability_rate'])})<br>"
                f"Mediana: {euro(r['median_ppn'])} (fascia {euro(r['p25_ppn'])}–{euro(r['p75_ppn'])})<br>"
                f"BnB: {own}"
                + (f" · indice {r['price_index']:.2f}" if pd.notna(r["price_index"]) else "")
                + f"<br>Rilevato il {r['observed_on']:%d/%m}")
        z.append(zr)
        text.append(tr)
        hover.append(hr)
    fig = go.Figure(go.Heatmap(
        z=z, x=WEEKDAYS, y=[f"sett. {pd.Timestamp(w):%d/%m}" for w in weeks], text=text,
        texttemplate="%{text}", hovertext=hover, hoverinfo="text", colorscale=SEQ_BLUE,
        zmin=0, zmax=1, xgap=2, ygap=2,
        colorbar=dict(title="Occupazione<br>stimata", tickformat=".0%")))
    fig.update_yaxes(autorange="reversed")
    fig.update_layout(**PLOT_LAYOUT, height=110 + 62 * len(weeks), title_text="")
    show(fig)
    st.caption("Colore: quota di concorrenti attivi non più prenotabili (più scuro = mercato più pieno). "
               "Nel riquadro: prezzo per notte del BnB e mediana del mercato.")

    with st.expander("Tabella"):
        t = s[["checkin", "n_available", "n_active", "availability_rate", "median_ppn", "p25_ppn", "p75_ppn",
               "own_ppn", "price_index", "own_percentile", "observed_on"]].copy()
        t.columns = ["Check-in", "Disponibili", "Attivi", "Disponibilità", "Mediana", "25°", "75°",
                     "BnB", "Indice", "Percentile BnB", "Rilevato"]
        st.dataframe(t, hide_index=True, use_container_width=True,
                     column_config={"Check-in": st.column_config.DateColumn(format="ddd DD/MM/YYYY"),
                                    "Rilevato": st.column_config.DateColumn(format="DD/MM"),
                                    "Disponibilità": st.column_config.NumberColumn(format="percent"),
                                    "Percentile BnB": st.column_config.NumberColumn(format="percent")})


def page_detail():
    st.header("Dettaglio data")
    if snap.empty:
        return no_data()
    dates = list(snap["checkin"].dt.date)
    checkin = st.selectbox("Data di check-in", dates,
                           format_func=lambda d: f"{WEEKDAYS[d.weekday()]} {d:%d/%m/%Y}")
    row = snap[snap["checkin"].dt.date == checkin].iloc[0]
    props = load_properties_for(profile, checkin, seg_group, radius_m)
    comp = props[~props["is_own"]] if not props.empty else props
    own = props[props["is_own"]] if not props.empty else props

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Disponibilità", pct(row["availability_rate"]),
              help=f"{int(row['n_available'])} concorrenti prenotabili su {int(row['n_active'])} attivi")
    c1.caption(f"{int(row['n_available'])} su {int(row['n_active'])} prenotabili")
    c2.metric("Mediana per notte", euro(row["median_ppn"]))
    c2.caption(f"fascia 25°–75°: {euro(row['p25_ppn'])}–{euro(row['p75_ppn'])}")
    c3.metric("Prezzo BnB per notte", euro(row["own_ppn"]) if pd.notna(row["own_ppn"]) else
              ("venduta" if row["own_available"] is False else "—"))
    c4.metric("Percentile BnB", pct(row["own_percentile"]),
              help="Quota di concorrenti disponibili più economici del BnB")

    left, right = st.columns(2)
    with left:
        av = comp[comp["available"] == True].dropna(subset=["ppn"])  # noqa: E712
        fig = go.Figure()
        fig.add_trace(go.Box(x=av["ppn"], name=seg_label, boxpoints="all", jitter=0.4, pointpos=0,
                             marker=dict(color=SERIES[seg_group], size=8), line=dict(width=2),
                             text=av["name"], hovertemplate="%{text}<br>€ %{x:.0f}/notte<extra></extra>",
                             orientation="h"))
        if not own.empty and pd.notna(own.iloc[0]["ppn"]):
            fig.add_vline(x=own.iloc[0]["ppn"], line=dict(color=OWN_COLOR, width=2),
                          annotation_text="BnB", annotation_position="top")
        fig.update_layout(**PLOT_LAYOUT, title="Distribuzione dei prezzi per notte", height=300,
                          showlegend=False, xaxis_title="€ per notte")
        show(fig)
    with right:
        rated = comp[(comp["available"] == True) & comp["ppn"].notna() & comp["rating"].notna()]  # noqa: E712
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=rated["rating"], y=rated["ppn"], mode="markers", name="Concorrenti",
                                 marker=dict(color=SERIES[seg_group], size=9, line=dict(color="white", width=1)),
                                 text=rated["name"],
                                 hovertemplate="%{text}<br>Voto %{x}<br>€ %{y:.0f}/notte<extra></extra>"))
        if len(rated) >= 5:
            k, b = np.polyfit(rated["rating"], rated["ppn"], 1)
            xs = [rated["rating"].min(), rated["rating"].max()]
            fig.add_trace(go.Scatter(x=xs, y=[k * x + b for x in xs], mode="lines", name="Tendenza",
                                     line=dict(color=MUTED, width=2, dash="dash"), hoverinfo="skip"))
            if not own.empty and pd.notna(own.iloc[0]["ppn"]) and pd.notna(own.iloc[0]["rating"]):
                exp = k * float(own.iloc[0]["rating"]) + b
                st.caption(f"Prezzo atteso dal mercato con voto {float(own.iloc[0]['rating']):.1f}: "
                           f"{euro(exp)} per notte (BnB: {euro(own.iloc[0]['ppn'])}).")
        if not own.empty and pd.notna(own.iloc[0]["ppn"]):
            o = own.iloc[0]
            fig.add_trace(go.Scatter(x=[o["rating"]], y=[o["ppn"]], mode="markers", name="BnB",
                                     marker=dict(color=OWN_COLOR, size=13, symbol="star",
                                                 line=dict(color="white", width=2)),
                                     hovertemplate="BnB<br>Voto %{x}<br>€ %{y:.0f}/notte<extra></extra>"))
        fig.update_layout(**PLOT_LAYOUT, title="Prezzo contro valutazione Booking", height=300,
                          xaxis_title="Voto Booking", yaxis_title="€ per notte")
        show(fig)

    hist = mk[mk["checkin"].dt.date == checkin].sort_values("observed_on")
    if len(hist) > 1:
        h1, h2 = st.columns(2)
        with h1:
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=hist["observed_on"], y=hist["p75_ppn"], mode="lines",
                                     line=dict(width=0), showlegend=False, hoverinfo="skip"))
            fig.add_trace(go.Scatter(x=hist["observed_on"], y=hist["p25_ppn"], mode="lines", fill="tonexty",
                                     fillcolor="rgba(42,120,214,0.15)", line=dict(width=0),
                                     name="Fascia 25°–75°", hoverinfo="skip"))
            fig.add_trace(go.Scatter(x=hist["observed_on"], y=hist["median_ppn"], mode="lines+markers",
                                     name="Mediana", line=dict(color=SERIES[seg_group], width=2),
                                     hovertemplate="%{x|%d/%m}: € %{y:.0f}<extra>Mediana</extra>"))
            fig.add_trace(go.Scatter(x=hist["observed_on"], y=hist["own_ppn"], mode="lines+markers",
                                     name="BnB", line=dict(color=OWN_COLOR, width=2),
                                     hovertemplate="%{x|%d/%m}: € %{y:.0f}<extra>BnB</extra>"))
            fig.update_layout(**PLOT_LAYOUT, title="Storia della mediana per questa data", height=300,
                              yaxis_title="€ per notte")
            show(fig)
        with h2:
            fig = go.Figure(go.Scatter(x=hist["observed_on"], y=hist["availability_rate"], mode="lines+markers",
                                       line=dict(color=SERIES[seg_group], width=2),
                                       hovertemplate="%{x|%d/%m}: %{y:.0%}<extra></extra>"))
            fig.update_layout(**PLOT_LAYOUT, title="Storia della disponibilità per questa data", height=300,
                              yaxis=dict(tickformat=".0%", rangemode="tozero"))
            show(fig)

    st.subheader("Concorrenti per distanza")
    t = comp.copy()
    if not t.empty:
        t["stato"] = t["available"].map({True: "disponibile", False: "non disponibile"}).fillna("non rilevata")
        t = t[["name", "segment", "distance_m", "stato", "ppn", "price_total", "rating", "review_count",
               "breakfast", "free_cancellation"]]
        t.columns = ["Struttura", "Segmento", "Distanza (m)", "Stato", "€/notte", "€ totale", "Voto",
                     "Recensioni", "Colazione", "Canc. gratuita"]
        st.dataframe(t, hide_index=True, use_container_width=True,
                     column_config={"Distanza (m)": st.column_config.NumberColumn(format="%.0f")})


def page_map():
    st.header("Mappa")
    if snap.empty:
        return no_data()
    c1, c2 = st.columns(2)
    dates = list(snap["checkin"].dt.date)
    checkin = c1.selectbox("Data di check-in", dates,
                           format_func=lambda d: f"{WEEKDAYS[d.weekday()]} {d:%d/%m/%Y}")
    mode = c2.radio("Colore", ["Prezzo per notte", "Disponibilità"], horizontal=True)
    props = load_properties_for(profile, checkin, seg_group, radius_m)
    props = props.dropna(subset=["lat"])
    comp = props[~props["is_own"]]
    own = props[props["is_own"]]
    olat, olon = (own.iloc[0]["lat"], own.iloc[0]["lon"]) if not own.empty else cfg.own_point

    fig = go.Figure()
    poly = cfg.polygon + [cfg.polygon[0]]
    fig.add_trace(go.Scattermapbox(lon=[p[0] for p in poly], lat=[p[1] for p in poly], mode="lines",
                                line=dict(color=MUTED, width=2), name="Zona centro", hoverinfo="skip"))
    if radius_m:
        circ = geo.circle(olat, olon, radius_m)
        fig.add_trace(go.Scattermapbox(lat=[c[0] for c in circ], lon=[c[1] for c in circ], mode="lines",
                                    line=dict(color=OWN_COLOR, width=1), name=f"Raggio {int(radius_m)} m",
                                    hoverinfo="skip"))
    hover = comp["name"] + "<br>" + comp["available"].map(
        {True: "disponibile", False: "non disponibile"}).fillna("non rilevata") + \
        comp["ppn"].map(lambda v: f"<br>€ {v:.0f}/notte" if pd.notna(v) else "")
    if mode == "Prezzo per notte":
        av = comp[comp["ppn"].notna()]
        na = comp[comp["ppn"].isna()]
        fig.add_trace(go.Scattermapbox(lat=na["lat"], lon=na["lon"], mode="markers", name="Non disponibile",
                                    marker=dict(size=9, color=MUTED), hovertext=hover[na.index], hoverinfo="text"))
        fig.add_trace(go.Scattermapbox(lat=av["lat"], lon=av["lon"], mode="markers", name="Disponibile",
                                    marker=dict(size=12, color=av["ppn"], colorscale=SEQ_BLUE, showscale=True,
                                                colorbar=dict(title="€/notte")),
                                    hovertext=hover[av.index], hoverinfo="text"))
    else:
        for state, color, label in ((True, SERIES["appartamenti"], "Disponibile"),
                                    (False, SERIES["camere"], "Non disponibile")):
            sub = comp[comp["available"] == state]
            fig.add_trace(go.Scattermapbox(lat=sub["lat"], lon=sub["lon"], mode="markers", name=label,
                                        marker=dict(size=11, color=color), hovertext=hover[sub.index],
                                        hoverinfo="text"))
    fig.add_trace(go.Scattermapbox(lat=[olat], lon=[olon], mode="markers", name="BnB",
                                marker=dict(size=18, color=OWN_COLOR), hovertext=["Il tuo BnB"],
                                hoverinfo="text"))
    fig.update_layout(mapbox=dict(style="carto-positron", center=dict(lat=(cfg.search_point[0] + olat) / 2, lon=(cfg.search_point[1] + olon) / 2),
                               zoom=14.0), height=650, margin=dict(l=0, r=0, t=0, b=0),
                      legend=dict(orientation="h", y=0.01, x=0.01, bgcolor="rgba(255,255,255,0.8)"))
    st.plotly_chart(fig, use_container_width=True)
    st.caption(f"{len(comp)} concorrenti con coordinate · {seg_label} · {cfg.profiles[profile].label}")


def page_trends():
    st.header("Tendenze")
    if market_all.empty:
        return no_data()
    prof = market_all[market_all["profile"] == profile].copy()
    prof["lead"] = (prof["checkin"] - prof["observed_on"]).dt.days

    near = prof[(prof["lead"] >= 0) & (prof["lead"] < 30)]
    trend = near.groupby(["observed_on", "seg_group"])["median_ppn"].median().reset_index()
    fig = go.Figure()
    for sg, color in SERIES.items():
        t = trend[trend["seg_group"] == sg]
        fig.add_trace(go.Scatter(x=t["observed_on"], y=t["median_ppn"], mode="lines+markers",
                                 name=metrics.SEG_GROUP_LABELS[sg], line=dict(color=color, width=2),
                                 hovertemplate="%{x|%d/%m}: € %{y:.0f}<extra>" + metrics.SEG_GROUP_LABELS[sg] + "</extra>"))
    fig.update_layout(**PLOT_LAYOUT, title="Mediana per notte nel tempo (soggiorni nei 30 giorni successivi)",
                      height=340, yaxis_title="€ per notte")
    show(fig)

    c1, c2 = st.columns(2)
    with c1:
        wk = metrics.weekend_gap_history(mk)
        fig = go.Figure()
        for kind, color, label in (("weekend", SERIES["camere"], "Venerdì e sabato"),
                                   ("weekday", SERIES["appartamenti"], "Infrasettimanale")):
            if kind in wk:
                fig.add_trace(go.Scatter(x=wk["observed_on"], y=wk[kind], mode="lines+markers", name=label,
                                         line=dict(color=color, width=2),
                                         hovertemplate="%{x|%d/%m}: € %{y:.0f}<extra>" + label + "</extra>"))
        gap = metrics.weekend_gap(snap)
        g = gap[(gap["profile"] == profile) & (gap["seg_group"] == seg_group)]
        title = "Weekend e infrasettimanale"
        if not g.empty and pd.notna(g.iloc[0]["gap_pct"]):
            title += f" · oggi weekend {g.iloc[0]['gap_pct']:+.0f}%"
        fig.update_layout(**PLOT_LAYOUT, title=title, height=320, yaxis_title="€ per notte")
        show(fig)
    with c2:
        curve = metrics.lead_curve(mk)
        pts = metrics.lead_points(curve)
        fig = go.Figure(go.Scatter(x=curve["lead_days"], y=curve["median_ppn"], mode="lines",
                                   line=dict(color=SERIES[seg_group], width=2), name="Mediana",
                                   hovertemplate="%{x} giorni: € %{y:.0f}<extra></extra>"))
        if not pts.empty:
            fig.add_trace(go.Scatter(x=pts["lead_days"], y=pts["median_ppn"], mode="markers+text",
                                     marker=dict(color=SERIES[seg_group], size=10, line=dict(color="white", width=2)),
                                     text=[euro(v) for v in pts["median_ppn"]], textposition="top center",
                                     name="60/30/14/3 giorni", hoverinfo="skip"))
        fig.update_layout(**PLOT_LAYOUT, title="Prezzo per giorni all'arrivo", height=320,
                          xaxis=dict(title="Giorni all'arrivo", autorange="reversed"), yaxis_title="€ per notte",
                          showlegend=False)
        show(fig)
        if mk["observed_on"].nunique() < 30:
            st.caption("La curva diventa affidabile dopo qualche settimana di storico.")

    mcfg = cfg.section("metrics")
    long_p, short_p = mcfg.get("primary_profile"), mcfg.get("single_night_profile")
    if long_p in cfg.profiles and short_p in cfg.profiles:
        c3, c4 = st.columns(2)
        with c3:
            ovt = metrics.one_vs_two_nights(market_all[market_all["seg_group"] == seg_group], long_p, short_p)
            if not ovt.empty:
                daily = ovt.groupby("observed_on")["gap_pct"].median().reset_index()
                fig = go.Figure(go.Scatter(x=daily["observed_on"], y=daily["gap_pct"], mode="lines+markers",
                                           line=dict(color=SERIES[seg_group], width=2),
                                           hovertemplate="%{x|%d/%m}: %{y:+.1f}%<extra></extra>"))
                fig.add_hline(y=0, line=dict(color=MUTED, width=1))
                fig.update_layout(**PLOT_LAYOUT, height=300, yaxis_title="%",
                                  title="1 notte rispetto a 2 notti (prezzo per notte, mediana)")
                show(fig)
        with c4:
            ms = load_min_stay(long_p, short_p, radius_m)
            if not ms.empty:
                ms = ms[ms["seg_group"] == seg_group]
                daily = (ms.groupby("observed_on")[["n_long_only", "n_long_available"]].sum()
                         .assign(share=lambda d: d["n_long_only"] / d["n_long_available"]).reset_index())
                fig = go.Figure(go.Scatter(x=daily["observed_on"], y=daily["share"], mode="lines+markers",
                                           line=dict(color=SERIES[seg_group], width=2),
                                           hovertemplate="%{x|%d/%m}: %{y:.0%}<extra></extra>"))
                fig.update_layout(**PLOT_LAYOUT, height=300, yaxis=dict(tickformat=".0%", rangemode="tozero"),
                                  title="Quota con soggiorno minimo 2 notti")
                show(fig)

    st.subheader("Dinamica a 7 giorni per data")
    ch = metrics.with_7d_change(mk)
    ch = ch[(ch["checkin"] >= today) & ch["ref_observed_on"].notna()]
    if ch.empty:
        st.caption("Servono almeno 7 giorni di storico.")
    else:
        t = ch[["checkin", "availability_rate", "avail_change_pts", "pickup_7d", "median_ppn",
                "median_change_pct", "own_ppn"]].copy()
        t.columns = ["Check-in", "Disponibilità", "Δ disponibilità (punti)", "Pickup 7 gg", "Mediana",
                     "Δ mediana %", "BnB"]
        st.dataframe(t, hide_index=True, use_container_width=True,
                     column_config={"Check-in": st.column_config.DateColumn(format="ddd DD/MM/YYYY"),
                                    "Disponibilità": st.column_config.NumberColumn(format="percent"),
                                    "Δ disponibilità (punti)": st.column_config.NumberColumn(format="%+.0f"),
                                    "Δ mediana %": st.column_config.NumberColumn(format="%+.1f")})


def page_status():
    st.header("Stato del sistema")
    runs = q("""SELECT id, observed_on, started_at, finished_at, status, n_searches, n_searches_complete,
                       n_cards, n_cards_priced, n_properties, n_new_properties, quality, error
                FROM runs ORDER BY id DESC LIMIT 60""")
    if runs.empty:
        st.info("Nessuna esecuzione registrata.")
    else:
        last = runs.iloc[0]
        icon = {"valid": "✅ valida", "suspect": "⚠️ sospetta", "failed": "❌ fallita",
                "blocked": "🛑 bloccata", "running": "⏳ in corso"}
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Ultima esecuzione", icon.get(last["status"], last["status"]))
        c2.metric("Iniziata", f"{pd.Timestamp(last['started_at']).tz_convert('Europe/Rome'):%d/%m %H:%M}")
        c3.metric("Ricerche complete", f"{last['n_searches_complete'] or 0}/{last['n_searches'] or 0}")
        c4.metric("Strutture trovate", int(last["n_properties"] or 0))
        reasons = (last["quality"] or {}).get("reasons") or []
        for r in reasons:
            st.warning(r)

        r = runs.sort_values("id")
        fig = go.Figure(go.Bar(x=r["observed_on"], y=r["n_properties"], marker_color=SERIES["appartamenti"],
                               customdata=r["status"].map(icon),
                               hovertemplate="%{x|%d/%m}: %{y} strutture<br>%{customdata}<extra></extra>"))
        fig.update_layout(**PLOT_LAYOUT, height=280, title="Strutture trovate per esecuzione")
        show(fig)

        t = runs[["id", "observed_on", "status", "n_searches", "n_searches_complete", "n_cards", "n_cards_priced",
                  "n_properties", "n_new_properties", "error"]].copy()
        t["status"] = t["status"].map(icon)
        t["error"] = t["error"].fillna("")
        t.columns = ["N.", "Data", "Esito", "Ricerche", "Complete", "Risultati", "Con prezzo", "Strutture",
                     "Nuove", "Errore"]
        st.dataframe(t, hide_index=True, use_container_width=True)

    st.subheader("Anagrafica")
    a = q("""SELECT count(*) AS totale,
                    count(*) FILTER (WHERE in_polygon) AS nel_centro,
                    count(*) FILTER (WHERE in_polygon AND active) AS attive_nel_centro,
                    count(*) FILTER (WHERE lat IS NULL) AS senza_coordinate,
                    count(*) FILTER (WHERE segment IS NULL) AS non_classificate
             FROM properties WHERE NOT is_own""")
    if not a.empty:
        r = a.iloc[0]
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Strutture note", int(r["totale"]))
        c2.metric("Nel centro", int(r["nel_centro"]))
        c3.metric("Attive nel centro", int(r["attive_nel_centro"]))
        c4.metric("Senza coordinate", int(r["senza_coordinate"]))
        c5.metric("Non classificate", int(r["non_classificate"]))
    seg = q("""SELECT COALESCE(segment, 'non classificata') AS segmento, count(*) AS strutture
               FROM properties WHERE in_polygon AND active AND NOT is_own GROUP BY 1 ORDER BY 2 DESC""")
    if not seg.empty:
        st.dataframe(seg, hide_index=True)
    unc = q("""SELECT booking_id, name, type_id, type_label, unit_text FROM properties
               WHERE segment IS NULL AND lat IS NOT NULL AND NOT is_own ORDER BY first_seen DESC LIMIT 50""")
    if not unc.empty:
        with st.expander("Strutture da classificare (aggiungere a classification.overrides)"):
            st.dataframe(unc, hide_index=True, use_container_width=True)


{"Calendario": page_calendar, "Dettaglio data": page_detail, "Mappa": page_map,
 "Tendenze": page_trends, "Stato del sistema": page_status}[page]()
