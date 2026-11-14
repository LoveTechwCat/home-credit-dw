-- DSS tables. Created empty by the ETL; dim_* are filled from settings by load.py,
-- fact_* by `hcdw score`. Grades are single letters, so 'A' < 'E' compares by risk.

CREATE TABLE dw.dim_risk_grade (
    grade                 VARCHAR PRIMARY KEY CHECK (grade IN ('A', 'B', 'C', 'D', 'E')),
    grade_rank            INTEGER NOT NULL UNIQUE CHECK (grade_rank BETWEEN 1 AND 5),
    min_rate              DOUBLE  NOT NULL CHECK (min_rate >= 0),          -- inclusive
    max_rate              DOUBLE  CHECK (max_rate IS NULL OR max_rate > min_rate),  -- exclusive; NULL for E
    label                 VARCHAR NOT NULL,
    decision              VARCHAR NOT NULL CHECK (decision IN ('approve', 'review', 'reject')),
    decision_label        VARCHAR NOT NULL,
    max_credit_to_income  DOUBLE  NOT NULL CHECK (max_credit_to_income >= 0)
);

CREATE TABLE dw.dim_rule (
    rule_code      VARCHAR PRIMARY KEY,
    rule_group     VARCHAR NOT NULL CHECK (rule_group IN ('segment', 'business')),
    label          VARCHAR NOT NULL,
    sql_condition  VARCHAR,              -- NULL for the segment rule (joined via mart.segment_risk)
    requires       VARCHAR[] NOT NULL,   -- input columns the rule needs (simulator: missing -> not evaluated)
    CHECK ((rule_group = 'segment') = (sql_condition IS NULL))
);

-- Grain: one scored application. Train rows use out-of-fold PD (no leakage), test rows the full model.
CREATE TABLE dw.fact_risk_assessment (
    application_id        BIGINT  PRIMARY KEY REFERENCES dw.fact_loan_application (application_id),
    pd                    DOUBLE  NOT NULL CHECK (pd BETWEEN 0 AND 1),     -- calibrated
    pd_source             VARCHAR NOT NULL CHECK (pd_source IN ('oof', 'model')),
    score_grade           VARCHAR NOT NULL REFERENCES dw.dim_risk_grade (grade),
    rule_grade            VARCHAR REFERENCES dw.dim_risk_grade (grade),    -- NULL: no rule violated
    final_grade           VARCHAR NOT NULL REFERENCES dw.dim_risk_grade (grade),
    grade_source          VARCHAR NOT NULL CHECK (grade_source IN ('score', 'rule', 'both')),
    downgraded            BOOLEAN NOT NULL,
    n_rule_hits           INTEGER NOT NULL CHECK (n_rule_hits >= 0),
    segment_key           VARCHAR,       -- NULL when a segment attribute is NULL
    segment_rate          DOUBLE  CHECK (segment_rate BETWEEN 0 AND 1),    -- kept even when not triggered,
                                         -- so policy simulation can re-grade segments under new thresholds
    decision              VARCHAR NOT NULL CHECK (decision IN ('approve', 'review', 'reject')),
    max_credit_to_income  DOUBLE  NOT NULL CHECK (max_credit_to_income >= 0),
    recommended_credit    DOUBLE  NOT NULL CHECK (recommended_credit >= 0),
    scored_at             TIMESTAMP NOT NULL,
    CHECK (final_grade >= score_grade),
    CHECK (rule_grade IS NULL OR final_grade >= rule_grade),
    CHECK (downgraded = (final_grade <> score_grade)),
    CHECK ((rule_grade IS NULL) = (n_rule_hits = 0))
);

-- Grain: one violated rule of one application. Business-rule hits are stored whatever their grade;
-- the segment rule only when its segment grade is in segment_trigger_grades.
CREATE TABLE dw.fact_rule_hit (
    application_id  BIGINT  NOT NULL REFERENCES dw.fact_risk_assessment (application_id),
    rule_code       VARCHAR NOT NULL REFERENCES dw.dim_rule (rule_code),
    rate            DOUBLE  NOT NULL CHECK (rate BETWEEN 0 AND 1),   -- train default rate of the violating group
    grade           VARCHAR NOT NULL REFERENCES dw.dim_risk_grade (grade),
    segment_key     VARCHAR,     -- BR-SEG-01 only
    rate_key        VARCHAR,     -- BR-SEG-01 only: group the rate came from ('*' = dropped attribute)
    PRIMARY KEY (application_id, rule_code)
);
