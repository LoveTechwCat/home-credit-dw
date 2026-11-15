-- Read by the dashboard and by src/dss/features.py.
-- Flattened star: one row per application with every dimension attribute, plus the DSS result
-- (pd .. recommended_credit), which stays NULL until `hcdw score` fills dw.fact_risk_assessment.
CREATE OR REPLACE VIEW mart.v_application_wide AS
SELECT
    f.*,
    c.sk_id_curr, c.gender, c.age_years, c.age_bucket, c.income_type, c.income_bracket,
    c.education_type, c.family_status, c.housing_type, c.occupation_type, c.organization_type,
    c.cnt_children, c.cnt_fam_members, c.own_car, c.own_realty, c.years_employed,
    c.is_employed_anomaly,
    h.bureau_active_bucket, h.max_dpd_bucket, h.has_current_overdue, h.has_bad_debt,
    h.prev_approval_bucket, h.credit_history_status,
    k.contract_type, k.accompanied_by, k.appr_weekday, k.appr_hour_band,
    r.region_rating_client, r.region_rating_city, r.population_density_band,
    r.reg_city_not_live_city, r.reg_city_not_work_city, r.reg_region_not_live_region,
    ra.pd, ra.score_grade, ra.rule_grade, ra.final_grade, ra.downgraded,
    ra.decision, ra.recommended_credit,
    ta.full_date                               AS application_date,
    ta.year_month                              AS application_month,     -- YYYY-MM
    format('{}-Q{}', ta.year, ta.quarter)      AS application_quarter,   -- YYYY-Qn
    ta.year                                    AS application_year,
    tout.full_date                             AS outcome_date,          -- NULL: result not known
    ((tout.full_date - ta.full_date) // 30)::INTEGER AS months_to_outcome  -- whole 30-day months
FROM dw.fact_loan_application f
JOIN dw.dim_customer       c USING (customer_key)
JOIN dw.dim_credit_history h USING (credit_history_key)
JOIN dw.dim_contract       k USING (contract_key)
JOIN dw.dim_region         r USING (region_key)
JOIN dw.dim_time           ta   ON ta.date_key   = f.application_date_key
LEFT JOIN dw.dim_time      tout ON tout.date_key = f.outcome_date_key
LEFT JOIN dw.fact_risk_assessment ra USING (application_id);  -- NULLs until `hcdw score` runs

-- Bureau credits with category, both calendar roles and customer attributes (time-series pages).
CREATE OR REPLACE VIEW mart.v_bureau_credit_wide AS
SELECT
    b.*,
    g.credit_active, g.credit_type, g.credit_currency,
    t.full_date AS open_date, t.year, t.quarter, t.year_month, t.month_offset,
    tc.full_date AS close_date, tc.year AS close_year,
    c.sk_id_curr, c.gender, c.age_bucket, c.income_type, c.income_bracket, c.occupation_type
FROM dw.fact_bureau_credit b
JOIN dw.dim_bureau_category g USING (bureau_category_key)
JOIN dw.dim_time     t  ON t.date_key  = b.open_date_key
LEFT JOIN dw.dim_time tc ON tc.date_key = b.close_date_key
JOIN dw.dim_customer c USING (customer_key);
