-- Metriche di mercato e posizione del BnB. Idempotente.

-- Metriche per data di rilevazione, profilo, data di soggiorno e gruppo di segmento.
-- p_radius_m NULL = tutta la zona centro; altrimenti solo strutture entro il raggio dal BnB.
CREATE OR REPLACE FUNCTION market_metrics(
    p_radius_m double precision DEFAULT NULL,
    p_from date DEFAULT NULL,
    p_to date DEFAULT NULL
) RETURNS TABLE (
    observed_on date,
    profile text,
    checkin date,
    seg_group text,
    n_active int,
    n_available int,
    availability_rate numeric,
    n_priced int,
    median_ppn numeric,
    p25_ppn numeric,
    p75_ppn numeric,
    own_available boolean,
    own_ppn numeric,
    price_index numeric,
    own_percentile numeric
) LANGUAGE sql STABLE AS $$
WITH comp AS (
    SELECT o.observed_on, o.profile, o.checkin, g.seg_group, o.available, o.price_per_night AS ppn
    FROM observations_daily o
    JOIN properties p ON p.booking_id = o.booking_id
    JOIN segment_groups g ON g.segment = p.segment
    WHERE p.in_polygon AND NOT p.is_own
      AND (p_radius_m IS NULL OR p.distance_m <= p_radius_m)
      AND (p_from IS NULL OR o.observed_on >= p_from)
      AND (p_to IS NULL OR o.observed_on <= p_to)
),
own AS (
    SELECT o.observed_on, o.profile, o.checkin, o.available, o.price_per_night AS ppn
    FROM observations_daily o
    JOIN properties p ON p.booking_id = o.booking_id
    WHERE p.is_own
      AND (p_from IS NULL OR o.observed_on >= p_from)
      AND (p_to IS NULL OR o.observed_on <= p_to)
),
agg AS (
    SELECT c.observed_on, c.profile, c.checkin, c.seg_group,
           count(*)::int AS n_active,
           count(*) FILTER (WHERE c.available)::int AS n_available,
           count(c.ppn) FILTER (WHERE c.available)::int AS n_priced,
           percentile_cont(0.5)  WITHIN GROUP (ORDER BY c.ppn) FILTER (WHERE c.available) AS median_ppn,
           percentile_cont(0.25) WITHIN GROUP (ORDER BY c.ppn) FILTER (WHERE c.available) AS p25_ppn,
           percentile_cont(0.75) WITHIN GROUP (ORDER BY c.ppn) FILTER (WHERE c.available) AS p75_ppn
    FROM comp c
    GROUP BY 1, 2, 3, 4
)
SELECT a.observed_on, a.profile, a.checkin, a.seg_group,
       a.n_active, a.n_available,
       round(a.n_available::numeric / NULLIF(a.n_active, 0), 4) AS availability_rate,
       a.n_priced,
       round(a.median_ppn::numeric, 2), round(a.p25_ppn::numeric, 2), round(a.p75_ppn::numeric, 2),
       own.available AS own_available,
       own.ppn AS own_ppn,
       round(own.ppn / NULLIF(a.median_ppn::numeric, 0), 3) AS price_index,
       (SELECT round(avg(CASE WHEN c.ppn < own.ppn THEN 1.0 WHEN c.ppn = own.ppn THEN 0.5 ELSE 0 END), 3)
          FROM comp c
         WHERE c.observed_on = a.observed_on AND c.profile = a.profile AND c.checkin = a.checkin
           AND c.seg_group = a.seg_group AND c.available AND c.ppn IS NOT NULL
           AND own.ppn IS NOT NULL) AS own_percentile
FROM agg a
LEFT JOIN own ON own.observed_on = a.observed_on AND own.profile = a.profile AND own.checkin = a.checkin
$$;

-- Riepilogo precalcolato per la zona centro (aggiornato dopo ogni esecuzione valida).
CREATE TABLE IF NOT EXISTS metrics_market (
    observed_on       date NOT NULL,
    profile           text NOT NULL,
    checkin           date NOT NULL,
    seg_group         text NOT NULL,
    n_active          int,
    n_available       int,
    availability_rate numeric,
    n_priced          int,
    median_ppn        numeric,
    p25_ppn           numeric,
    p75_ppn           numeric,
    own_available     boolean,
    own_ppn           numeric,
    price_index       numeric,
    own_percentile    numeric,
    PRIMARY KEY (observed_on, profile, checkin, seg_group)
);

-- Quota di strutture con soggiorno minimo di 2 notti: disponibili nella ricerca lunga
-- ma assenti (non disponibili) nella ricerca da 1 notte, stessa data e stessa rilevazione.
CREATE OR REPLACE FUNCTION min_stay_share(
    p_long_profile text,
    p_short_profile text,
    p_radius_m double precision DEFAULT NULL
) RETURNS TABLE (
    observed_on date,
    checkin date,
    seg_group text,
    n_long_available int,
    n_long_only int,
    share numeric
) LANGUAGE sql STABLE AS $$
SELECT l.observed_on, l.checkin, g.seg_group,
       count(*)::int,
       count(*) FILTER (WHERE NOT s.available)::int,
       round(count(*) FILTER (WHERE NOT s.available)::numeric / NULLIF(count(*), 0), 4)
FROM observations_daily l
JOIN observations_daily s
  ON s.observed_on = l.observed_on AND s.checkin = l.checkin
 AND s.booking_id = l.booking_id AND s.profile = p_short_profile
JOIN properties p ON p.booking_id = l.booking_id
JOIN segment_groups g ON g.segment = p.segment
WHERE l.profile = p_long_profile AND l.available
  AND p.in_polygon AND NOT p.is_own
  AND (p_radius_m IS NULL OR p.distance_m <= p_radius_m)
GROUP BY 1, 2, 3
$$;
