const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

export const pct = (v: unknown, digits = 1) => (isNum(v) ? `${(v * 100).toFixed(digits)}%` : "–");

/** Fixed decimals with an optional suffix ("1.82×"); "–" for null / NaN so API gaps never crash a render. */
export const fixed = (v: unknown, digits = 2, suffix = "") => (isNum(v) ? `${v.toFixed(digits)}${suffix}` : "–");

export const num = (v: unknown, digits = 0) =>
  isNum(v) ? v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "–";

/** 1,284 / 12.9K / 4.2M / 1.3B */
export function compact(v: unknown, digits = 1): string {
  if (!isNum(v)) return "–";
  const a = Math.abs(v);
  if (a >= 1e9) return `${(v / 1e9).toFixed(digits)}B`;
  if (a >= 1e6) return `${(v / 1e6).toFixed(digits)}M`;
  if (a >= 1e4) return `${(v / 1e3).toFixed(digits)}K`;
  return num(v, 0);
}

export function byKind(kind: string, v: unknown): string {
  switch (kind) {
    case "percent":
      return pct(v, 1);
    case "count":
      return num(v);
    case "money":
      return compact(v);
    default:
      return isNum(v) ? v.toFixed(3) : "–";
  }
}

/** Title-case the all-caps weekday members etc. for display only. */
export const label = (s: string) => (/^[A-Z]{3,}$/.test(s) ? s[0] + s.slice(1).toLowerCase() : s);

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})/;

/** "2016-03-14" (or an ISO timestamp) → "14 Mar 2016". Parsed by hand so no time zone can shift the day. */
export function dateLabel(v: unknown): string {
  if (typeof v !== "string") return "–";
  const m = ISO_DATE.exec(v);
  if (!m) return v;
  return `${Number(m[3])} ${MONTHS[Number(m[2]) - 1] ?? m[2]} ${m[1]}`;
}

/** True for a "YYYY-MM-DD" string that is a real calendar date. */
export function isIsoDate(v: unknown): v is string {
  if (typeof v !== "string") return false;
  const m = ISO_DATE.exec(v);
  if (!m || v.length !== 10) return false;
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
  return d.getUTCFullYear() === +m[1] && d.getUTCMonth() === +m[2] - 1 && d.getUTCDate() === +m[3];
}

/** Calendar dimensions whose members must be shown in time order, never by value. */
export const TIME_DIMS = new Set(["application_year", "application_quarter", "application_month"]);

/** Sortable number for "2016", "2016-03", "2016-Q1", "Q1 2016", "Mar 2016" or "2016 Mar"; null if not a period. */
function periodKey(m: string): number | null {
  const y = /(19|20)\d{2}/.exec(m);
  if (!y) return null;
  const year = Number(y[0]);
  const q = /Q([1-4])/i.exec(m);
  if (q) return year * 100 + Number(q[1]) * 3;
  const mon = MONTHS.findIndex((x) => new RegExp(x, "i").test(m));
  if (mon >= 0) return year * 100 + mon + 1;
  const mm = /^(?:19|20)\d{2}-(\d{2})/.exec(m);
  if (mm) return year * 100 + Number(mm[1]);
  return year * 100;
}

/** Order members of a time dimension chronologically (other members, e.g. "ALL", keep their place at the end). */
export function sortTime<T>(dim: string, items: T[], member: (t: T) => string): T[] {
  if (!TIME_DIMS.has(dim)) return items;
  const keyed = items.map((t) => ({ t, k: periodKey(member(t)) }));
  const dated = keyed.filter((x) => x.k != null).sort((a, b) => (a.k as number) - (b.k as number));
  return [...dated, ...keyed.filter((x) => x.k == null)].map((x) => x.t);
}
