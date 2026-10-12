-- Schema del Monitor prezzi BnB Trieste. Idempotente: si applica a ogni avvio.

-- Esecuzioni notturne e loro esito.
CREATE TABLE IF NOT EXISTS runs (
    id              bigserial PRIMARY KEY,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    observed_on     date NOT NULL,
    status          text NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'valid', 'suspect', 'failed', 'blocked')),
    n_searches      int,
    n_searches_complete int,
    n_cards         int,
    n_cards_priced  int,
    n_cards_sold_out int,
    n_properties    int,
    n_new_properties int,
    quality         jsonb NOT NULL DEFAULT '{}'::jsonb,
    error           text,
    promoted        boolean NOT NULL DEFAULT false
);

-- Area di appoggio: ricerche eseguite (servono per sapere quali assenze contano).
CREATE TABLE IF NOT EXISTS staging_searches (
    run_id       bigint NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    profile      text NOT NULL,
    checkin      date NOT NULL,
    pages        int NOT NULL DEFAULT 0,
    n_cards      int NOT NULL DEFAULT 0,
    complete     boolean NOT NULL DEFAULT false,
    stop_reason  text,
    error        text,
    PRIMARY KEY (run_id, profile, checkin)
);

-- Area di appoggio: risultati grezzi.
CREATE TABLE IF NOT EXISTS staging_results (
    run_id            bigint NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    profile           text NOT NULL,
    checkin           date NOT NULL,
    booking_id        text NOT NULL,
    name              text,
    list_position     int,
    price_shown       numeric(10,2),
    taxes_extra       numeric(10,2),
    price_total       numeric(10,2),
    price_includes    text,
    rating            numeric(3,1),
    review_count      int,
    breakfast         boolean,
    free_cancellation boolean,
    sold_out          boolean NOT NULL DEFAULT false,
    distance_text_m   numeric(10,1),
    unit_text         text,
    scraped_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, profile, checkin, booking_id)
);

-- Anagrafica strutture.
CREATE TABLE IF NOT EXISTS properties (
    booking_id        text PRIMARY KEY,
    name              text,
    url               text,
    type_id           int,
    type_label        text,
    unit_text         text,
    segment           text CHECK (segment IN ('apartment', 'room_extra', 'room_hotel', 'other')),
    lat               double precision,
    lon               double precision,
    in_polygon        boolean,
    distance_m        double precision,
    rating            numeric(3,1),
    review_count      int,
    first_seen        timestamptz NOT NULL DEFAULT now(),
    last_seen         timestamptz,
    active            boolean NOT NULL DEFAULT true,
    is_own            boolean NOT NULL DEFAULT false,
    details_fetched_at timestamptz,
    details_error     text
);
CREATE INDEX IF NOT EXISTS properties_competitors_idx ON properties (in_polygon, active, segment);

-- Osservazioni: una riga per struttura, profilo e data di soggiorno, solo da esecuzioni valide.
CREATE TABLE IF NOT EXISTS observations (
    run_id            bigint NOT NULL REFERENCES runs(id),
    observed_on       date NOT NULL,
    profile           text NOT NULL,
    checkin           date NOT NULL,
    nights            int NOT NULL,
    booking_id        text NOT NULL REFERENCES properties(booking_id),
    available         boolean NOT NULL,
    price_total       numeric(10,2),
    price_per_night   numeric(10,2),
    price_includes    text,
    breakfast         boolean,
    free_cancellation boolean,
    list_position     int,
    rating            numeric(3,1),
    review_count      int,
    PRIMARY KEY (run_id, profile, checkin, booking_id)
);
CREATE INDEX IF NOT EXISTS observations_lookup_idx ON observations (observed_on, profile, checkin);
CREATE INDEX IF NOT EXISTS observations_property_idx ON observations (booking_id, profile, checkin);

-- Gruppi di segmento usati da dashboard e metriche.
CREATE TABLE IF NOT EXISTS segment_groups (
    segment   text NOT NULL,
    seg_group text NOT NULL,
    PRIMARY KEY (segment, seg_group)
);
INSERT INTO segment_groups (segment, seg_group) VALUES
    ('apartment',  'appartamenti'),
    ('room_extra', 'camere'),
    ('room_hotel', 'camere'),
    ('room_extra', 'camere_no_hotel')
ON CONFLICT DO NOTHING;

-- Messaggi inviati (evita doppi riepiloghi e allarmi).
CREATE TABLE IF NOT EXISTS notifications (
    id        bigserial PRIMARY KEY,
    sent_at   timestamptz NOT NULL DEFAULT now(),
    kind      text NOT NULL,
    ref       text,
    text      text,
    ok        boolean NOT NULL
);

-- Ultima osservazione del giorno (se in una giornata ci sono più esecuzioni valide).
CREATE OR REPLACE VIEW observations_daily AS
SELECT DISTINCT ON (o.observed_on, o.profile, o.checkin, o.booking_id) o.*
FROM observations o
ORDER BY o.observed_on, o.profile, o.checkin, o.booking_id, o.run_id DESC;
