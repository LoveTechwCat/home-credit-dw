-- Calendar position of every application, from the optional application_dates source, as day offsets
-- relative to the snapshot date ({{anchor_date}}). One row per application:
--   * no source row (or the file is absent): the application sits on the snapshot date, no outcome;
--   * duplicate or unparseable source rows are reported by quality.py; here the earliest parseable
--     value wins so the joins downstream stay one row per application.
CREATE OR REPLACE TABLE staging.application_calendar AS
WITH src AS (
    SELECT sk_id_curr,
           MIN(TRY_CAST(application_date AS DATE)) AS application_date,
           MIN(TRY_CAST(outcome_date AS DATE))     AS outcome_date
    FROM staging.application_dates
    WHERE sk_id_curr IS NOT NULL
    GROUP BY sk_id_curr
)
SELECT
    a.sk_id_curr,
    COALESCE(s.application_date - DATE '{{anchor_date}}', 0)::INTEGER AS application_offset,
    (s.outcome_date - DATE '{{anchor_date}}')::INTEGER                AS outcome_offset
FROM (SELECT DISTINCT sk_id_curr FROM staging.application) a
LEFT JOIN src s USING (sk_id_curr);
