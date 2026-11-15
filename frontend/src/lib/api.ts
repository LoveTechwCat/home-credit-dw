import { useEffect, useState } from "react";

/** Response cache shared by GET and POST hooks, capped as an LRU so long Explorer sessions stay small. */
const CACHE_MAX = 100;
const store = new Map<string, unknown>();
const cache = {
  has: (k: string) => store.has(k),
  get(k: string): unknown {
    const v = store.get(k);
    store.delete(k);
    store.set(k, v); // most recently used goes last
    return v;
  },
  set(k: string, v: unknown) {
    store.delete(k);
    store.set(k, v);
    if (store.size > CACHE_MAX) store.delete(store.keys().next().value as string);
  },
};

/** Error carrying the HTTP status, so pages can tell 404 / 400 / 503 apart. */
export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

/** FastAPI sends `detail` as a string (HTTPException) or a list of {loc, msg} (422 validation). */
function detailText(detail: unknown): string | undefined {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        const loc = Array.isArray(d?.loc) ? d.loc.filter((x: unknown) => x !== "body").join(".") : "";
        return loc ? `${loc}: ${d?.msg ?? ""}` : String(d?.msg ?? d);
      })
      .join("; ");
  }
  return undefined;
}

async function toApiError(res: Response): Promise<ApiError> {
  let detail = `${res.status} ${res.statusText}`;
  try {
    detail = detailText((await res.json()).detail) ?? detail;
  } catch {
    /* keep the status line */
  }
  return new ApiError(detail, res.status);
}

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal });
  if (!res.ok) throw await toApiError(res);
  return res.json() as Promise<T>;
}

export interface ApiState<T> {
  data: T | undefined;
  error: string | undefined;
  /** HTTP status of the failed request (undefined for network errors). */
  status?: number;
  loading: boolean;
}

/** GET with an in-memory cache (the warehouse is a static snapshot) and request cancellation. */
export function useApi<T>(url: string | null): ApiState<T> {
  const [state, setState] = useState<ApiState<T>>(() =>
    url && cache.has(url) ? { data: cache.get(url) as T, error: undefined, loading: false }
      : { data: undefined, error: undefined, loading: !!url },
  );

  useEffect(() => {
    if (!url) return;
    if (cache.has(url)) {
      setState({ data: cache.get(url) as T, error: undefined, loading: false });
      return;
    }
    const ctrl = new AbortController();
    setState((s) => ({ data: s.data, error: undefined, loading: true }));
    getJson<T>(url, ctrl.signal)
      .then((data) => {
        cache.set(url, data);
        setState({ data, error: undefined, loading: false });
      })
      .catch((e: Error) => {
        if (e.name !== "AbortError") {
          setState({ data: undefined, error: e.message, status: e instanceof ApiError ? e.status : undefined, loading: false });
        }
      });
    return () => ctrl.abort();
  }, [url]);

  return state;
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`/api/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await toApiError(res);
  return res.json() as Promise<T>;
}

/**
 * POST whose body is part of the request identity (explore metrics / applications). Results are cached by
 * path + body like GETs (static snapshot); a newer body supersedes an in-flight one, whose answer is dropped.
 * Keeps the previous data while loading so tables don't flash empty between pages.
 */
export function usePostApi<T>(path: string, body: unknown | null): ApiState<T> {
  const key = body == null ? null : `POST ${path} ${JSON.stringify(body)}`;
  const [state, setState] = useState<ApiState<T>>(() =>
    key && cache.has(key) ? { data: cache.get(key) as T, error: undefined, loading: false }
      : { data: undefined, error: undefined, loading: !!key },
  );

  useEffect(() => {
    if (!key) return;
    if (cache.has(key)) {
      setState({ data: cache.get(key) as T, error: undefined, loading: false });
      return;
    }
    let live = true;
    setState((s) => ({ data: s.data, error: undefined, loading: true }));
    postJson<T>(path, body)
      .then((data) => {
        cache.set(key, data);
        if (live) setState({ data, error: undefined, loading: false });
      })
      .catch((e: Error) => {
        if (live) setState({ data: undefined, error: e.message, status: e instanceof ApiError ? e.status : undefined, loading: false });
      });
    return () => { live = false; };
    // `key` already encodes path + body, so it is the only dependency that matters.
  }, [key]);

  return state;
}

// ---- response types -------------------------------------------------------------------------
export interface Meta {
  warehouse: { target: string; name: string; anchor_date: string; tables: { table_name: string; n_rows: number }[] };
  dimensions: { key: string; label: string }[];
  filter_dims: string[];
  measures: { key: string; label: string; kind: string }[];
  /** Drill paths, coarse → fine (e.g. year → quarter → month); optional until the API exposes them. */
  hierarchies?: Record<string, string[]> | string[][];
  members: Record<string, string[]>;
}

export interface DimRow {
  member: string;
  n_applications: number;
  n_labelled: number;
  default_rate: number | null;
  overdue_share: number | null;
  avg_credit: number | null;
  median_dti: number | null;
  total_credit: number | null;
}

// ---- DSS (A-E grades) - contract in docs/dss_design.md section 7 ------------------------------
export const GRADES = ["A", "B", "C", "D", "E"] as const;
export type Grade = (typeof GRADES)[number];
export type Decision = "approve" | "review" | "reject";
export type GradeType = "score" | "rule" | "final";

/** One SHAP factor; `shap` is in log-odds (> 0 raises the risk, < 0 lowers it). */
export interface ShapFactor {
  feature: string;
  label: string;
  value: number | string | boolean | null;
  shap: number;
}

export interface RuleHit {
  code: string;
  group: "segment" | "business";
  label: string;
  rate: number;
  grade: Grade;
  segment_key: string | null;
  rate_key: string | null;
}

export interface RuleNotEvaluated {
  code: string;
  group: "segment" | "business";
  label: string;
  missing: string[];
}

/** Shared "assessment" shape returned by GET /api/assessment/{id}. */
export interface Assessment {
  application_id: number | null;
  is_train: boolean | null;
  target: number | null;
  pd: number;
  pd_source: "oof" | "model";
  score_grade: Grade;
  rule_grade: Grade | null;
  final_grade: Grade;
  grade_source: "score" | "rule" | "both";
  downgraded: boolean;
  summary: string;
  decision: Decision;
  decision_label: string;
  max_credit_to_income: number;
  amt_income: number;
  amt_credit: number;
  credit_to_income: number;
  recommended_credit: number;
  shap: { up: ShapFactor[]; down: ShapFactor[] };
  rule_hits: RuleHit[];
  missing_fields: string[];
  rules_not_evaluated: RuleNotEvaluated[];
  /** Applicant facts used for scoring (values may be null). */
  profile: AssessmentProfile;
}

export interface AssessmentProfile {
  age_years: number | null;
  income_type: string | null;
  occupation_type: string | null;
  education_type: string | null;
  family_status: string | null;
  housing_type: string | null;
  contract_type: string | null;
  region_rating_client: number | null;
  credit_history_status: string | null;
  ext_source_mean: number | null;
  years_employed: number | null;
  cnt_children: number | null;
  cnt_fam_members: number | null;
  amt_income: number | null;
  amt_credit: number | null;
  amt_annuity: number | null;
  amt_goods_price: number | null;
  cnt_bureau_active: number | null;
  max_dpd_days: number | null;
  late_payment_ratio: number | null;
  prev_approval_rate: number | null;
  has_current_overdue: boolean | null;
  /** ISO dates (null for new applications); a repaid loan's outcome date means "repaid as of". */
  application_date: string | null;
  outcome_date: string | null;
  months_to_outcome: number | null;
}

export interface GradeShareRow {
  /** null only in the `rule` distribution: no rule violated. */
  grade: Grade | null;
  n: number;
  share: number;
  default_rate: number | null;
}

export interface GradePolicyRow {
  grade: Grade;
  label: string;
  min_rate: number;
  max_rate: number | null;
  decision: Decision;
  decision_label: string;
  max_credit_to_income: number;
}

export interface GradesResponse {
  scope: "train" | "test" | "all";
  n: number;
  thresholds: number[];
  policy: GradePolicyRow[];
  distribution: Record<GradeType, GradeShareRow[]>;
  downgraded_share: number;
  approval_rate: number;
  referral_rate: number;
  reject_rate: number;
  approved_default_rate: number | null;
}

export interface PolicySimRequest {
  thresholds: number[];
  policy: Record<Grade, { decision: Decision; max_credit_to_income: number }>;
  grade_type: "final" | "score";
  /** Only the rules that differ from the configured ones. */
  rules?: Record<string, { enabled: boolean; value?: number }>;
}

/** GET /api/rules: the underwriting rule catalog in config order. `param` is null for toggle-only rules. */
export interface RuleDef {
  code: string;
  group: "segment" | "business";
  label: string;
  param: { field: string; op: string; value: number; unit: "ratio" | "days"; min: number; max: number; step: number } | null;
  n_violators: number;
  defaults: number;
  default_rate: number | null;
  grade: Grade | null;
}

/** Per-rule result of a simulation (for the simulated rule set). */
export interface SimRule {
  code: string;
  enabled: boolean;
  value: number | null;
  /** Historical loans hit (the base of default_rate). */
  n_violators: number;
  /** All scored applications hit. */
  n_violators_all?: number;
  default_rate: number | null;
  grade: Grade | null;
}

export interface PolicySimResponse {
  grade_type: "final" | "score";
  n: number;
  approval_rate: number;
  referral_rate: number;
  reject_rate: number;
  approved_default_rate: number | null;
  /** Σ recommended (capped) credit of approved applications. */
  approved_credit: number;
  /** Σ requested credit of the same approved applications. */
  approved_credit_requested: number;
  /** Σ recommended credit of applications sent to manual review. */
  review_credit: number;
  /** Σ recommended credit × PD of approved applications (added with the Playground). */
  approved_expected_loss: number;
  by_grade: {
    grade: Grade; n: number; share: number; default_rate: number | null; decision: Decision;
    credit_volume: number; requested_credit: number; expected_loss: number;
  }[];
  /** Groups whose grade or decision differs from the configured policy (n desc). */
  transitions: PolicyTransition[];
  /** Rule results for the simulated rule set (stats filled even for disabled rules: what they would catch). */
  rules: SimRule[];
}

export interface PolicyTransition {
  from_grade: Grade;
  to_grade: Grade;
  from_decision: Decision;
  to_decision: Decision;
  /** All scored applications in the group; defaults and default_rate use its historical loans. */
  n: number;
  n_historical?: number;
  defaults: number;
  default_rate: number | null;
  requested_credit: number;
  credit_at_risk: number;
}

export interface GlobalExplain {
  auc_oof: number;
  brier: number;
  n_train: number;
  gender_in_features: boolean;
  features: { feature: string; label: string; mean_abs_shap: number }[];
  calibration: { bin: number; n: number; mean_pd: number; default_rate: number }[];
}

/** GET /api/kpis/summary: unfiltered headline KPIs over all scored applications (default rates on historical loans); query params ignored. */
export interface KpiSummary {
  n_applications: number;
  n_labelled: number;
  n_scored: number;
  default_rate: number | null;
  downgraded_share: number | null;
  total_credit: number | null;
  expected_loss: number | null;
  avg_pd: number | null;
  approval_rate: number | null;
  approved_default_rate: number | null;
  referral_rate: number | null;
  reject_rate: number | null;
  auc: number | null;
  median_debt_to_income: number | null;
  median_credit_to_income: number | null;
}

// ---- Explorer (cohort analysis) -----------------------------------------------------------------
export type FieldType = "category" | "number" | "boolean" | "date";
export type CohortOp =
  | "is" | "is_not" | "is_one_of" | "is_not_one_of" | "exists" | "missing"
  | "eq" | "neq" | "gt" | "gte" | "lt" | "lte" | "between";
/** Date fields use ISO "YYYY-MM-DD" strings (a pair for "between"). */
export type CohortValue = string | string[] | number | [number, number] | [string, string] | boolean | null;

export interface ExploreField {
  key: string;
  label: string;
  group: string;
  type: FieldType;
  ops: CohortOp[];
  values?: string[];
  /** Numbers for numeric fields, ISO dates for date fields. */
  min?: number | string;
  max?: number | string;
}

/** One cohort condition as sent to the API. */
export interface CohortCondition {
  field: string;
  op: CohortOp;
  value: CohortValue;
}

export interface CohortMetrics {
  n_applications: number;
  n_historical: number;
  n_new: number;
  /** null for the book itself. */
  share_of_book: number | null;
  total_credit: number;
  auto_approve_rate: number | null;
  manual_review_rate: number | null;
  reject_rate: number | null;
  remaining_risk: {
    approved_historical: number;
    approved_defaults: number;
    approved_default_rate: number | null;
    approved_expected_loss: number | null;
  };
  blocked_risk: { defaults_historical: number; defaults_blocked: number; blocked_share: number | null };
  /** Good (repaid) / bad (defaulted) historical loans per decision. */
  outcome_by_decision: Record<"approve" | "review" | "reject", { good: number; bad: number }>;
  manual_review: {
    review_historical: number;
    review_repaid: number;
    review_repaid_share: number | null;
    review_defaulted: number;
    review_defaulted_share: number | null;
  };
}

export interface ExploreApplicationsResponse {
  total: number;
  page: number;
  page_size: number;
  rows: (Record<string, string | number | boolean | null> & {
    application_id: number;
    loan_set: string;
    final_grade: Grade | null;
    /** Decision label ("Approve" / "Manual review" / "Reject"), not the code. */
    decision: string | null;
    pd: number | null;
    outcome: string | null;
  })[];
}

// ---- Analytics (OLAP cube) ------------------------------------------------------------------------
export interface CubeRequest {
  conditions: CohortCondition[];
  rows: string[];
  cols: string[];
  measure: string;
}

/** One cell; a null member is the subtotal / total level of that dimension (GROUPING SETS). */
export interface CubeCell {
  r: (string | null)[];
  c: string | null;
  value: number | null;
  n: number;
}

export interface CubeResponse {
  rows: string[];
  cols: string[];
  measure: { key: string; label: string; format: string };
  n: number;
  members: Record<string, string[]>;
  cells: CubeCell[];
  hierarchies?: Record<string, string[]> | string[][];
}
