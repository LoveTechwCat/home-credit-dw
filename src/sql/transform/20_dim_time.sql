-- Calendar around the assumed snapshot date. The range always covers every application and outcome
-- date and the oldest bureau credit (bureau offsets count from the applicant's application date).
INSERT INTO dw.dim_time
WITH cal AS (
    SELECT
        MIN(LEAST(application_offset, COALESCE(outcome_offset, application_offset)))    AS lo,
        MAX(GREATEST(application_offset, COALESCE(outcome_offset, application_offset))) AS hi
    FROM staging.application_calendar
),
bounds AS (
    SELECT
        LEAST(-{{history_days}}, COALESCE(cal.lo, 0),
              COALESCE((SELECT MIN(COALESCE(c.application_offset, 0) + b.days_credit)
                        FROM staging.bureau b
                        LEFT JOIN staging.application_calendar c USING (sk_id_curr)), 0)) AS lo,
        GREATEST({{future_days}}, COALESCE(cal.hi, 0))                                   AS hi
    FROM cal
),
offsets AS (
    SELECT UNNEST(generate_series(lo, hi))::INTEGER AS day_offset FROM bounds
),
days AS (
    SELECT day_offset, DATE '{{anchor_date}}' + day_offset AS d FROM offsets
)
SELECT
    CAST(strftime(d, '%Y%m%d') AS INTEGER)              AS date_key,
    d                                                   AS full_date,
    day_offset,
    datediff('month', DATE '{{anchor_date}}', d)        AS month_offset,
    dayofmonth(d)                                       AS day_of_month,
    month(d)                                            AS month_num,
    monthname(d)                                        AS month_name,
    quarter(d)                                          AS quarter,
    year(d)                                             AS year,
    strftime(d, '%Y-%m')                                AS year_month,
    dayofweek(d) IN (0, 6)                              AS is_weekend
FROM days
ORDER BY day_offset;
