-- Segment risk table for the segment rule (BR-SEG-01). One row per combination of the four
-- segment attributes; small segments borrow the rate of their parent level. Train rows only.
CREATE OR REPLACE VIEW mart.segment_risk AS
WITH base AS (
    SELECT is_train, target, credit_history_status,
           {{dss_age_band_sql}}     AS age_band,
           {{dss_income_group_sql}} AS income_group,
           region_rating_client
    FROM mart.v_application_wide
),
train AS (SELECT * FROM base WHERE is_train),
-- every combination seen in any application, so test/new rows always find a row
domain AS (
    SELECT *
    FROM       (SELECT DISTINCT credit_history_status FROM base WHERE credit_history_status IS NOT NULL)
    CROSS JOIN (SELECT DISTINCT age_band              FROM base WHERE age_band IS NOT NULL)
    CROSS JOIN (SELECT DISTINCT income_group          FROM base WHERE income_group IS NOT NULL)
    CROSS JOIN (SELECT DISTINCT region_rating_client  FROM base WHERE region_rating_client IS NOT NULL)
),
l4 AS (SELECT credit_history_status, age_band, income_group, region_rating_client,
              COUNT(*) AS n, AVG(target) AS dr FROM train GROUP BY ALL),
l3 AS (SELECT credit_history_status, age_band, income_group,
              COUNT(*) AS n, AVG(target) AS dr FROM train GROUP BY ALL),
l2 AS (SELECT credit_history_status, age_band, COUNT(*) AS n, AVG(target) AS dr FROM train GROUP BY ALL),
l1 AS (SELECT credit_history_status, COUNT(*) AS n, AVG(target) AS dr FROM train GROUP BY ALL),
l0 AS (SELECT COUNT(*) AS n, AVG(target) AS dr FROM train),
picked AS (
    SELECT d.*,
           COALESCE(l4.n, 0) AS n,
           CASE WHEN l4.n >= {{segment_min_n}} THEN 4
                WHEN l3.n >= {{segment_min_n}} THEN 3
                WHEN l2.n >= {{segment_min_n}} THEN 2
                WHEN l1.n >= {{segment_min_n}} THEN 1
                ELSE 0 END AS level,
           l4.n AS n4, l4.dr AS dr4, l3.n AS n3, l3.dr AS dr3, l2.n AS n2, l2.dr AS dr2,
           l1.n AS n1, l1.dr AS dr1, l0.n AS n0, l0.dr AS dr0
    FROM domain d
    LEFT JOIN l4 USING (credit_history_status, age_band, income_group, region_rating_client)
    LEFT JOIN l3 USING (credit_history_status, age_band, income_group)
    LEFT JOIN l2 USING (credit_history_status, age_band)
    LEFT JOIN l1 USING (credit_history_status)
    CROSS JOIN l0
),
rated AS (
    SELECT *,
           CASE level WHEN 4 THEN n4 WHEN 3 THEN n3 WHEN 2 THEN n2 WHEN 1 THEN n1 ELSE n0 END AS rate_n,
           CASE level WHEN 4 THEN dr4 WHEN 3 THEN dr3 WHEN 2 THEN dr2 WHEN 1 THEN dr1 ELSE dr0 END AS default_rate
    FROM picked
)
SELECT
    concat_ws('|', r.credit_history_status, r.age_band, r.income_group,
              CAST(r.region_rating_client AS VARCHAR))                       AS segment_key,
    r.credit_history_status, r.age_band, r.income_group, r.region_rating_client,
    r.n, r.level,
    concat_ws('|',
        CASE WHEN r.level >= 1 THEN r.credit_history_status ELSE '*' END,
        CASE WHEN r.level >= 2 THEN r.age_band ELSE '*' END,
        CASE WHEN r.level >= 3 THEN r.income_group ELSE '*' END,
        CASE WHEN r.level >= 4 THEN CAST(r.region_rating_client AS VARCHAR) ELSE '*' END) AS rate_key,
    r.rate_n, r.default_rate, g.grade,
    COALESCE(g.grade IN ({{dss_trigger_grades_sql}}), FALSE)               AS is_trigger
FROM rated r
LEFT JOIN dw.dim_risk_grade g
       ON r.default_rate >= g.min_rate AND (g.max_rate IS NULL OR r.default_rate < g.max_rate);

-- Rule statistics (train only): violators, their default rate, and the grade of that rate.
CREATE OR REPLACE VIEW mart.rule_stats AS
WITH src AS (
    SELECT *, {{dss_age_band_sql}} AS age_band, {{dss_income_group_sql}} AS income_group
    FROM mart.v_application_wide
    WHERE is_train
),
seg_hits AS (
    SELECT s.application_id, d.rule_code
    FROM src s
    JOIN mart.segment_risk r
      ON  r.credit_history_status = s.credit_history_status
      AND r.age_band              = s.age_band
      AND r.income_group          = s.income_group
      AND r.region_rating_client  = s.region_rating_client
    CROSS JOIN (SELECT rule_code FROM dw.dim_rule WHERE rule_group = 'segment') d
    WHERE r.is_trigger
),
hits AS (
    SELECT application_id, rule_code FROM seg_hits
    UNION ALL
    {{dss_rule_hits_sql}}
),
stats AS (
    SELECT d.rule_code, d.rule_group, d.label,
           COUNT(h.application_id) AS n_violators,
           AVG(t.target)           AS default_rate
    FROM dw.dim_rule d
    LEFT JOIN hits h USING (rule_code)
    LEFT JOIN (SELECT application_id, target FROM src) t USING (application_id)
    GROUP BY ALL
)
SELECT s.*, g.grade
FROM stats s
LEFT JOIN dw.dim_risk_grade g
       ON s.default_rate >= g.min_rate AND (g.max_rate IS NULL OR s.default_rate < g.max_rate);
