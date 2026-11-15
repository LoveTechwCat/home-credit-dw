import type { CohortCondition, CohortOp, CohortValue, ExploreField } from "./api";
import { dateLabel, isIsoDate, label, num } from "./format";

/** A cohort condition in the UI: the API condition plus an on/off switch (disabled = kept but not applied). */
export interface UiCondition extends CohortCondition {
  enabled: boolean;
}

/** URL parameter holding the cohort, so an Explorer view can be shared as a link. */
export const COHORT_PARAM = "cohort";

export const OP_LABEL: Record<CohortOp, string> = {
  is: "is", is_not: "is not", is_one_of: "is one of", is_not_one_of: "is not one of",
  exists: "exists", missing: "is missing",
  eq: "=", neq: "≠", gt: ">", gte: "≥", lt: "<", lte: "≤", between: "is between",
};

/** Date fields read naturally: "Application date on or after 1 Jan 2016". */
const DATE_OP_LABEL: Partial<Record<CohortOp, string>> = {
  eq: "on", neq: "not on", gt: "after", gte: "on or after", lt: "before", lte: "on or before", between: "between",
  exists: "has date", missing: "no date",
};
export const opLabel = (op: CohortOp, field?: ExploreField) =>
  (field?.type === "date" ? DATE_OP_LABEL[op] : undefined) ?? OP_LABEL[op];

/** Operators that take no value. */
export const NO_VALUE_OPS: CohortOp[] = ["exists", "missing"];

/** Link to the Explorer pre-filtered to the given conditions (all enabled). */
export function exploreHref(conditions: CohortCondition[]): string {
  if (!conditions.length) return "/explore";
  const q = new URLSearchParams({ [COHORT_PARAM]: JSON.stringify(conditions.map((c) => ({ ...c, enabled: true }))) });
  return `/explore?${q.toString()}`;
}

/** Parse the cohort from the URL; anything malformed is dropped rather than breaking the page. */
export function readCohort(raw: string | null): UiCondition[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((c) => c && typeof c.field === "string" && typeof c.op === "string" && Object.prototype.hasOwnProperty.call(OP_LABEL, c.op))
      .map((c) => ({ field: c.field, op: c.op as CohortOp, value: (c.value ?? null) as CohortValue, enabled: c.enabled !== false }));
  } catch {
    return [];
  }
}

export function writeCohort(conds: UiCondition[]): string | null {
  return conds.length ? JSON.stringify(conds) : null;
}

/** Conditions actually sent to the API. */
export const activeConditions = (conds: UiCondition[]): CohortCondition[] =>
  conds.filter((c) => c.enabled).map(({ field, op, value }) => ({ field, op, value }));

/** Display a value of one field: categories title-cased, numbers grouped, booleans Yes/No, dates "14 Mar 2016". */
export function formatFieldValue(v: CohortValue | undefined, field?: ExploreField): string {
  if (v == null) return "";
  if (field?.type === "date") return Array.isArray(v) ? `${dateLabel(v[0])} – ${dateLabel(v[1])}` : dateLabel(v);
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "number") return num(v, Number.isInteger(v) ? 0 : 2);
  if (Array.isArray(v)) {
    if (v.length === 2 && typeof v[0] === "number") return `${formatFieldValue(v[0])} – ${formatFieldValue(v[1] as number)}`;
    const list = (v as string[]).map((x) => label(String(x)));
    return list.length > 3 ? `${list.slice(0, 3).join(", ")} +${list.length - 3}` : list.join(", ");
  }
  return label(String(v));
}

/** "Employment type is one of Working, Pensioner" */
export function conditionText(c: CohortCondition, field?: ExploreField): string {
  const name = field?.label ?? c.field;
  if (NO_VALUE_OPS.includes(c.op)) return `${name} ${opLabel(c.op, field)}`;
  return `${name} ${opLabel(c.op, field)} ${formatFieldValue(c.value, field)}`;
}

/** Returns an error message, or null when the condition can be applied. */
export function validateCondition(c: CohortCondition, field?: ExploreField): string | null {
  if (field && !field.ops.includes(c.op)) return "This operator does not apply to the field.";
  if (NO_VALUE_OPS.includes(c.op)) return null;
  const v = c.value;
  // Category values must come from the catalog (a hand-edited link could carry anything).
  if (field?.type === "category" && field.values?.length) {
    const known = new Set(field.values);
    const list = Array.isArray(v) ? v : [v];
    if (list.some((x) => typeof x !== "string" || !known.has(x))) return "Unknown value for this field.";
  }
  if (field?.type === "date") {
    if (c.op === "between") {
      if (!Array.isArray(v) || v.length !== 2 || !v.every(isIsoDate)) return "Pick both dates.";
      return (v[0] as string) <= (v[1] as string) ? null : "The first date must not be after the second.";
    }
    return isIsoDate(v) ? null : "Pick a date.";
  }
  switch (c.op) {
    case "is_one_of":
    case "is_not_one_of":
      return Array.isArray(v) && v.length ? null : "Pick at least one value.";
    case "between": {
      if (!Array.isArray(v) || v.length !== 2 || !v.every((x) => typeof x === "number" && Number.isFinite(x))) return "Enter both numbers.";
      return (v[0] as number) <= (v[1] as number) ? null : "The first number must not be larger than the second.";
    }
    case "is":
    case "is_not":
      if (field?.type === "boolean") return typeof v === "boolean" ? null : "Pick Yes or No.";
      return typeof v === "string" && v !== "" ? null : "Pick a value.";
    default:
      return typeof v === "number" && Number.isFinite(v) ? null : "Enter a number.";
  }
}

/**
 * Check every condition against the field catalog. Invalid ones (unknown field, operator or value, e.g. from
 * an edited link) are reported by index and never sent to the API.
 */
export function checkCohort(conds: UiCondition[], fields: ExploreField[]): Set<number> {
  const byKey = new Map(fields.map((f) => [f.key, f]));
  const bad = new Set<number>();
  conds.forEach((c, i) => {
    const f = byKey.get(c.field);
    if (!f || validateCondition(c, f)) bad.add(i);
  });
  return bad;
}
