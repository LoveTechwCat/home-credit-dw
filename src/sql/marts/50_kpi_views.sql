-- KPI-01..05 and secondary metrics SM-1..5 (docs/business_requirements.md §6), described in docs/kpi_definitions.md.
-- Scope (same rule as the web API): counts, grade mix, decision rates, credit and credit at risk
-- (amt_credit x pd) cover every scored application, historical and new; outcome measures (default
-- rates, AUC) cover the labelled (train) rows only, since new applications have no TARGET.
-- KPIs that need the DSS result (pd, grades, decision) read
-- dw.fact_risk_assessment through mart.v_application_wide: they are NULL (rates, sums) or 0 (counts)
-- until `hcdw score` has run, and never fail.

-- SM-4: AUC of the PD model on out-of-fold predictions (rank / Mann-Whitney form, ties count 1/2).
CREATE OR REPLACE VIEW mart.kpi_model_auc AS
WITH s AS (
    SELECT a.pd, f.target
    FROM dw.fact_risk_assessment a
    JOIN dw.fact_loan_application f USING (application_id)
    WHERE f.is_train AND a.pd_source = 'oof' AND f.target IS NOT NULL
),
ranked AS (
    SELECT target, AVG(rn) OVER (PARTITION BY pd) AS avg_rank   -- tied PDs share their mean rank
    FROM (SELECT *, ROW_NUMBER() OVER (ORDER BY pd) AS rn FROM s)
)
SELECT
    COUNT(*)                     AS n,
    COALESCE(SUM(target), 0)     AS n_default,
    (SUM(avg_rank) FILTER (WHERE target = 1) - SUM(target)::DOUBLE * (SUM(target) + 1) / 2)
        / NULLIF(SUM(target)::DOUBLE * (COUNT(*) - SUM(target)), 0) AS auc
FROM ranked;

-- One-row portfolio summary (Overview KPIs, /api/kpis/summary). Every application; outcome
-- measures FILTER on is_train.
CREATE OR REPLACE VIEW mart.kpi_portfolio_summary AS
SELECT
    COUNT(*)                                                  AS n_applications,
    COUNT(*) FILTER (WHERE is_train)                          AS n_labelled,
    COUNT(final_grade)                                        AS n_scored,
    AVG(target) FILTER (WHERE is_train)                       AS default_rate,            -- KPI-01 (outcome)
    AVG(downgraded::INTEGER)                                  AS downgraded_share,        -- SM-1
    SUM(amt_credit)                                           AS total_credit,            -- KPI-02
    SUM(amt_credit * pd)                                      AS expected_loss,           -- KPI-03
    AVG(pd)                                                   AS avg_pd,                  -- SM-3
    AVG(CASE WHEN decision = 'approve' THEN 1.0 WHEN decision IS NOT NULL THEN 0.0 END)
                                                              AS approval_rate,           -- KPI-04
    AVG(target) FILTER (WHERE is_train AND decision = 'approve')
                                                              AS approved_default_rate,   -- SM-2 (outcome)
    AVG(CASE WHEN decision = 'review' THEN 1.0 WHEN decision IS NOT NULL THEN 0.0 END)
                                                              AS referral_rate,           -- KPI-05
    AVG(CASE WHEN decision = 'reject' THEN 1.0 WHEN decision IS NOT NULL THEN 0.0 END)
                                                              AS reject_rate,
    (SELECT auc FROM mart.kpi_model_auc)                      AS auc,                     -- SM-4 (outcome)
    MEDIAN(debt_to_income)                                    AS median_debt_to_income,   -- SM-5
    MEDIAN(credit_to_income)                                  AS median_credit_to_income  -- SM-5
FROM mart.v_application_wide;

-- KPI-01..03, SM-1, SM-3 and SM-5 per grade, for each grade type. Every grade A-E is listed even when empty.
-- Every scored application; default_rate on the labelled (train) rows only.
-- share = n / scored applications: it adds up to 100% for score and final grades (SM-1); for the
-- rule grade the remainder is the applications that violate no rule.
CREATE OR REPLACE VIEW mart.kpi_grade_distribution AS
WITH p AS (SELECT * FROM mart.v_application_wide),
graded AS (
    SELECT t.grade_type,
           CASE t.grade_type WHEN 'score' THEN p.score_grade
                             WHEN 'rule'  THEN p.rule_grade
                             ELSE p.final_grade END AS grade,
           p.is_train, p.target, p.amt_credit, p.pd, p.downgraded, p.debt_to_income, p.credit_to_income
    FROM p CROSS JOIN (VALUES ('score'), ('rule'), ('final')) t(grade_type)
),
totals AS (SELECT COUNT(final_grade) AS n_scored FROM p)
SELECT
    t.grade_type, g.grade, g.grade_rank, g.label, g.decision,
    COUNT(x.grade)                                        AS n,
    COUNT(x.grade) / NULLIF(MAX(tot.n_scored), 0)         AS share,                -- SM-1
    COUNT(*) FILTER (WHERE x.downgraded)                  AS n_downgraded,         -- SM-1
    AVG(x.target) FILTER (WHERE x.is_train)               AS default_rate,         -- KPI-01 (outcome)
    SUM(x.amt_credit)                                     AS total_credit,         -- KPI-02
    SUM(x.amt_credit * x.pd)                              AS expected_loss,        -- KPI-03
    AVG(x.pd)                                             AS avg_pd,               -- SM-3
    MEDIAN(x.debt_to_income)                              AS median_debt_to_income,   -- SM-5
    MEDIAN(x.credit_to_income)                            AS median_credit_to_income  -- SM-5
FROM (VALUES ('score'), ('rule'), ('final')) t(grade_type)
CROSS JOIN dw.dim_risk_grade g
CROSS JOIN totals tot
LEFT JOIN graded x ON x.grade_type = t.grade_type AND x.grade = g.grade
GROUP BY ALL;

-- KPI-01 by attribute group: age bucket x income bracket x employment type (outcome measure: train only).
CREATE OR REPLACE VIEW mart.kpi_default_by_segment AS
SELECT
    age_bucket,
    income_bracket,
    income_type,
    COUNT(*)               AS n_labelled,
    AVG(target)            AS default_rate,
    MEDIAN(debt_to_income) AS median_debt_to_income
FROM mart.v_application_wide
WHERE is_train
GROUP BY ALL;

-- KPI-01 by credit history status (first attribute of the segment rule; outcome measure: train only).
CREATE OR REPLACE VIEW mart.kpi_default_by_credit_history AS
SELECT
    credit_history_status,
    COUNT(*)                           AS n_labelled,
    COUNT(*) / SUM(COUNT(*)) OVER ()   AS share,
    AVG(target)                        AS default_rate
FROM mart.v_application_wide
WHERE is_train
GROUP BY ALL;
