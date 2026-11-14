import clsx from "clsx";
import { ArrowLeftRight, Check, ChevronDown, ChevronUp, Code2, Copy, Play, Plus, Table2, X } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { DssError } from "../components/AssessmentView";
import { useGradeColors } from "../components/charts";
import { CohortBar } from "../components/CohortBar";
import { Card, ChartSkeleton, ErrorBox, PageHeader, Segmented, TooltipCard } from "../components/ui";
import {
  useApi, usePostApi, type CohortCondition, type CubeCell, type CubeRequest, type CubeResponse, type ExploreField,
} from "../lib/api";
import { activeConditions, checkCohort, exploreHref, readCohort, type UiCondition } from "../lib/cohort";
import { byKind, label, num, sortTime, TIME_DIMS } from "../lib/format";
import { useMeta } from "../lib/meta";
import { seqColor, useTheme } from "../lib/theme";

/** Fallback drill path when the API does not expose hierarchies (coarse → fine). */
const TIME_PATH = ["application_year", "application_quarter", "application_month"];

type Interval = "application_year" | "application_quarter" | "application_month";
const INTERVALS: { value: Interval; label: string }[] = [
  { value: "application_year", label: "Year" }, { value: "application_quarter", label: "Quarter" }, { value: "application_month", label: "Month" },
];
interface View {
  rows: string[]; cols: string[]; measure: string; conditions: UiCondition[];
  interval: Interval; split: string;
}
const DEFAULT_VIEW: View = {
  rows: ["application_year"], cols: ["final_grade"], measure: "default_rate", conditions: [],
  interval: "application_month", split: "credit_history_status",
};

/**
 * View state lives in ?state=… so any analysis can be shared as a link. Everything is validated against the
 * API's dimensions and measures, so a hand-edited or stale link can never produce a request that fails.
 */
function readView(raw: string | null, dimKeys: string[], measureKeys: string[]): View {
  const dims = new Set(dimKeys);
  const fallbackSplit = dims.has(DEFAULT_VIEW.split) ? DEFAULT_VIEW.split : (dimKeys.find((d) => !TIME_DIMS.has(d)) ?? DEFAULT_VIEW.split);
  const base: View = {
    ...DEFAULT_VIEW,
    rows: DEFAULT_VIEW.rows.filter((d) => dims.has(d)),
    cols: DEFAULT_VIEW.cols.filter((d) => dims.has(d)),
    measure: measureKeys.includes(DEFAULT_VIEW.measure) ? DEFAULT_VIEW.measure : (measureKeys[0] ?? DEFAULT_VIEW.measure),
    split: fallbackSplit,
  };
  if (!raw) return base;
  let v: unknown;
  try { v = JSON.parse(raw); } catch { return base; }
  if (!v || typeof v !== "object" || Array.isArray(v)) return base;
  const o = v as Record<string, unknown>;
  // Known dimensions only, no dimension twice across rows and columns, at most 2 rows and 1 column.
  const seen = new Set<string>();
  const pick = (x: unknown, max: number) => (Array.isArray(x) ? x : [])
    .filter((d): d is string => typeof d === "string" && dims.has(d) && !seen.has(d) && (seen.add(d), true))
    .slice(0, max);
  const rows = pick(o.rows, 2);
  const cols = pick(o.cols, 1);
  return {
    rows,
    cols,
    measure: typeof o.measure === "string" && measureKeys.includes(o.measure) ? o.measure : base.measure,
    conditions: readCohort(JSON.stringify(Array.isArray(o.conditions) ? o.conditions : [])),
    // Older links may still carry chart / pct: they are simply ignored.
    interval: INTERVALS.some((x) => x.value === o.interval) && dims.has(o.interval as string) ? (o.interval as Interval) : base.interval,
    split: typeof o.split === "string" && dims.has(o.split) && !TIME_DIMS.has(o.split) ? o.split : base.split,
  };
}

/** Analytics (/analytics): an OLAP cube with slice/dice (filter bar), roll-up/drill-down and pivot. */
export default function Analytics() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "sql" ? "sql" : "cube";
  const setTab = (t: "cube" | "sql") => {
    const next = new URLSearchParams(params);
    if (t === "sql") next.set("tab", "sql"); else next.delete("tab");
    setParams(next, { replace: true });
  };
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <PageHeader title="Analytics" />
        <Segmented<"cube" | "sql"> ariaLabel="View" value={tab} onChange={setTab}
          options={[{ value: "cube", label: "Cube" }, { value: "sql", label: "SQL queries" }]} />
      </div>
      {tab === "cube" ? <Cube /> : <SavedQueries />}
    </div>
  );
}

// ---- cube ---------------------------------------------------------------------------------------------

function Cube() {
  const meta = useMeta();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const rawState = params.get("state");
  const dimKeys = useMemo(() => meta.dimensions.map((d) => d.key), [meta.dimensions]);
  const measureKeys = useMemo(() => meta.measures.map((m) => m.key), [meta.measures]);
  const view = useMemo(() => readView(rawState, dimKeys, measureKeys), [rawState, dimKeys, measureKeys]);
  const setView = (patch: Partial<View>) => {
    const next = new URLSearchParams(params);
    next.set("state", JSON.stringify({ ...view, ...patch }));
    setParams(next, { replace: true });
  };

  const fields = useApi<{ fields: ExploreField[] }>("/api/explore/fields");
  const catalog = useMemo(() => fields.data?.fields ?? [], [fields.data]);
  const ready = !!fields.data || !!fields.error;
  const invalid = useMemo(() => (fields.data ? checkCohort(view.conditions, catalog) : new Set<number>()), [fields.data, view.conditions, catalog]);
  const active = useMemo(() => activeConditions(view.conditions.filter((_, i) => !invalid.has(i))), [view.conditions, invalid]);

  const dims = meta.dimensions;
  const has = (k: string) => dims.some((d) => d.key === k);
  const dimLabel = (k: string) => dims.find((d) => d.key === k)?.label ?? label(k);
  // "decision" may not exist yet in the cube: fall back to the final grade.
  const splitDim = has("decision") ? "decision" : "final_grade";
  const segDim = has(view.split) && !TIME_DIMS.has(view.split) ? view.split : "credit_history_status";
  const t = view.interval;

  // All panel requests go out together (one hook each); each keeps its previous answer while reloading.
  const q = (rows: string[], cols: string[], measure: string): CubeRequest | null => (ready ? { conditions: active, rows, cols, measure } : null);
  const volume = usePostApi<CubeResponse>("analytics/cube", q([t], [splitDim], "n_applications"));
  const rate = usePostApi<CubeResponse>("analytics/cube", q([t], [], "default_rate"));
  const mix = usePostApi<CubeResponse>("analytics/cube", q([t], ["final_grade"], "n_applications"));
  const seg = usePostApi<CubeResponse>("analytics/cube", q([segDim], [], "default_rate"));
  const pivot = usePostApi<CubeResponse>("analytics/cube", q(view.rows, view.cols, view.measure));
  const paths = useMemo(() => hierarchyPaths(pivot.data?.hierarchies ?? meta.hierarchies), [pivot.data?.hierarchies, meta.hierarchies]);

  // Drill-through: the clicked members become Explorer conditions on top of the current slice/dice.
  const drillThrough = (members: [string, string | null][]) => {
    const known = new Set(catalog.map((f) => f.key));
    // "(missing)" is the cube's label for a real NULL member: it becomes an "is missing" condition.
    const extra: CohortCondition[] = members.filter(([d, m]) => m != null && known.has(d))
      .map(([d, m]) => (m === "(missing)" ? { field: d, op: "missing", value: null } : { field: d, op: "is", value: m as string }));
    navigate(exploreHref([...active, ...extra]));
  };

  return (
    <div className="space-y-5">
      {fields.error ? <DssError status={fields.status} message={fields.error} />
        : <CohortBar fields={catalog} conditions={view.conditions} invalid={invalid} onChange={(conditions) => setView({ conditions })} />}

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-semibold text-ink-2">Interval</span>
        <Segmented<Interval> ariaLabel="Time interval" value={t} onChange={(interval) => setView({ interval })} options={INTERVALS} />
      </div>

      <Panel title="Applications over time" res={volume}>
        {(d) => <TimeStack data={d} dim={t} split={splitDim} pct={false} onPick={drillThrough} />}
      </Panel>
      <div className="grid gap-5 xl:grid-cols-2">
        <Panel title="Default rate over time" res={rate}>
          {(d) => <RateLine data={d} dim={t} onPick={drillThrough} />}
        </Panel>
        <Panel title="Grade mix over time" res={mix}>
          {(d) => <TimeStack data={d} dim={t} split="final_grade" pct onPick={drillThrough} />}
        </Panel>
      </div>
      <Panel title="Default rate by segment" res={seg}
        actions={
          <label className="flex items-center gap-2 text-[12.5px] font-semibold text-ink-2">
            Split by
            <select className="input w-auto cursor-pointer py-1 text-[13px]" value={segDim} onChange={(e) => setView({ split: e.target.value })}>
              {dims.filter((d) => !TIME_DIMS.has(d.key)).map((d) => <option key={d.key} value={d.key}>{d.label}</option>)}
            </select>
          </label>
        }>
        {(d) => <SegmentBars data={d} dim={segDim} onPick={drillThrough} />}
      </Panel>

      <PivotSection view={view} setView={setView} res={pivot} dims={dims} paths={paths} dimLabel={dimLabel} onCell={drillThrough} />
    </div>
  );
}

/** A panel that keeps its previous chart (dimmed) while a new answer loads; skeleton only before the first one. */
function Panel({ title, res, actions, children }: {
  title: string;
  res: { data?: CubeResponse; error?: string; status?: number; loading: boolean };
  actions?: ReactNode;
  children: (d: CubeResponse) => ReactNode;
}) {
  return (
    <Card title={title} actions={actions}>
      {res.error ? <DssError status={res.status} message={res.error} />
        : !res.data ? <ChartSkeleton height={240} />
        : !res.data.n ? <p className="py-6 text-center text-[13px] text-ink-2">No applications match.</p>
        : <div className={clsx("transition-opacity duration-150", res.loading && "opacity-60")} aria-busy={res.loading}>{children(res.data)}</div>}
    </Card>
  );
}

function PivotSection({ view, setView, res, dims, paths, dimLabel, onCell }: {
  view: View;
  setView: (p: Partial<View>) => void;
  res: { data?: CubeResponse; error?: string; status?: number; loading: boolean };
  dims: { key: string; label: string }[];
  paths: string[][];
  dimLabel: (k: string) => string;
  onCell: (members: [string, string | null][]) => void;
}) {
  const meta = useMeta();
  const used = [...view.rows, ...view.cols];
  const free = dims.filter((d) => !used.includes(d.key));
  const replace = (axis: "rows" | "cols", i: number, dim: string) => setView({ [axis]: view[axis].map((d, j) => (j === i ? dim : d)) });
  const remove = (axis: "rows" | "cols", i: number) => setView({ [axis]: view[axis].filter((_, j) => j !== i) });
  const add = (dim: string) => {
    if (view.rows.length < 2) setView({ rows: [...view.rows, dim] });
    else if (view.cols.length < 1) setView({ cols: [dim] });
  };
  // Pivot: the first row dimension and the column dimension trade places.
  const swap = () => setView({ rows: [...view.cols, ...view.rows.slice(1)], cols: view.rows.slice(0, 1) });
  return (
    <Card title="Cube" actions={res.data ? <span className="num text-[12.5px] text-ink-2">{num(res.data.n)} applications</span> : undefined}>
      <div className="mb-4 flex flex-wrap items-center gap-x-6 gap-y-3">
        <Axis title="Rows">
          {view.rows.map((d, i) => (
            <DimChip key={`r${i}`} dim={d} dims={dims} used={used} paths={paths} dimLabel={dimLabel}
              onChange={(x) => replace("rows", i, x)} onRemove={() => remove("rows", i)} />
          ))}
        </Axis>
        {/* Pivot swaps one row dimension with the column; with two row dimensions there is no single swap. */}
        <button type="button" className="chip" onClick={swap} disabled={(!view.rows.length && !view.cols.length) || view.rows.length === 2}
          aria-label={view.rows.length === 2 ? "Swap rows and columns (remove a row dimension first)" : "Swap rows and columns"}
          title={view.rows.length === 2 ? "Remove a row dimension to swap" : "Swap rows and columns"}>
          <ArrowLeftRight className="h-3.5 w-3.5" aria-hidden />
        </button>
        <Axis title="Columns">
          {view.cols.map((d, i) => (
            <DimChip key={`c${i}`} dim={d} dims={dims} used={used} paths={paths} dimLabel={dimLabel}
              onChange={(x) => replace("cols", i, x)} onRemove={() => remove("cols", i)} />
          ))}
        </Axis>
        {(view.rows.length < 2 || view.cols.length < 1) && free.length > 0 && (
          <label className="chip relative cursor-pointer">
            <Plus className="h-3.5 w-3.5" aria-hidden /> Add dimension
            <select className="absolute inset-0 cursor-pointer opacity-0" value="" aria-label="Add dimension"
              onChange={(e) => { if (e.target.value) add(e.target.value); }}>
              <option value="">Add dimension</option>
              {free.map((d) => <option key={d.key} value={d.key}>{d.label}</option>)}
            </select>
          </label>
        )}
        <label className="ml-auto flex items-center gap-2 text-[13px] font-semibold text-ink-2">
          Measure
          <select className="input w-auto cursor-pointer py-1" value={view.measure} onChange={(e) => setView({ measure: e.target.value })}>
            {meta.measures.map((m) => <option key={m.key} value={m.key}>{m.label}</option>)}
          </select>
        </label>
      </div>
      {res.error ? <DssError status={res.status} message={res.error} />
        : !res.data ? <ChartSkeleton height={240} />
        : (
          <div className={clsx("transition-opacity duration-150", res.loading && "opacity-60")} aria-busy={res.loading}>
            <CubeTable data={res.data} dimLabel={dimLabel} onCell={onCell} />
          </div>
        )}
    </Card>
  );
}

/** Parse hierarchies from the API (either {name: [dims]} or [[dims]]); always includes the time path. */
function hierarchyPaths(h: Record<string, string[]> | string[][] | undefined): string[][] {
  const list = !h ? [] : Array.isArray(h) ? h : Object.values(h);
  const paths = list.filter((p) => Array.isArray(p) && p.length > 1);
  return paths.some((p) => p.includes("application_month")) ? paths : [...paths, TIME_PATH];
}

function Axis({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-[12.5px] font-semibold text-ink-2">{title}</span>
      {children}
      {!Array.isArray(children) || (children as unknown[]).length === 0 ? <span className="text-[12.5px] text-ink-3">none</span> : null}
    </div>
  );
}

/** Dimension chip: picker, ▲ roll-up / ▼ drill-down along its hierarchy, × drop the dimension (roll-up). */
function DimChip({ dim, dims, used, paths, dimLabel, onChange, onRemove }: {
  dim: string;
  dims: { key: string; label: string }[];
  used: string[];
  paths: string[][];
  dimLabel: (k: string) => string;
  onChange: (d: string) => void;
  onRemove: () => void;
}) {
  const path = paths.find((p) => p.includes(dim));
  const at = path ? path.indexOf(dim) : -1;
  const up = path && at > 0 ? path[at - 1] : null;
  const down = path && at >= 0 && at < path.length - 1 ? path[at + 1] : null;
  const free = (d: string | null) => d != null && !used.includes(d);
  return (
    <span className="inline-flex items-center gap-0.5 rounded-full border-[1.5px] border-accent bg-surface py-0.5 pl-2 pr-1 text-[12.5px]">
      <select className="max-w-[170px] cursor-pointer truncate bg-transparent font-semibold text-ink outline-none" value={dim}
        aria-label="Dimension" onChange={(e) => onChange(e.target.value)}>
        {dims.filter((d) => d.key === dim || !used.includes(d.key)).map((d) => <option key={d.key} value={d.key}>{d.label}</option>)}
      </select>
      {path && (
        <>
          <button type="button" className="rounded-full p-1 text-ink-2 hover:bg-raised hover:text-ink disabled:opacity-30"
            disabled={!free(up)} onClick={() => up && onChange(up)} aria-label={up ? `Roll up to ${dimLabel(up)}` : "Top level"} title={up ? `Roll up to ${dimLabel(up)}` : undefined}>
            <ChevronUp className="h-3.5 w-3.5" aria-hidden />
          </button>
          <button type="button" className="rounded-full p-1 text-ink-2 hover:bg-raised hover:text-ink disabled:opacity-30"
            disabled={!free(down)} onClick={() => down && onChange(down)} aria-label={down ? `Drill down to ${dimLabel(down)}` : "Lowest level"} title={down ? `Drill down to ${dimLabel(down)}` : undefined}>
            <ChevronDown className="h-3.5 w-3.5" aria-hidden />
          </button>
        </>
      )}
      <button type="button" className="rounded-full p-1 text-ink-2 hover:bg-raised hover:text-ink" onClick={onRemove}
        aria-label={`Remove ${dimLabel(dim)}`} title={`Remove ${dimLabel(dim)}`}>
        <X className="h-3.5 w-3.5" aria-hidden />
      </button>
    </span>
  );
}

// ---- result table ---------------------------------------------------------------------------------

const key = (r: (string | null)[], c: string | null) => JSON.stringify([r, c]);

/**
 * Heatmap table: body cells shaded on one ramp, an "All" row / column (and per-group subtotals) from the
 * cube's subtotal cells. Clicking a cell drills through to its applications in the Explorer.
 */
function CubeTable({ data, dimLabel, onCell }: {
  data: CubeResponse;
  dimLabel: (k: string) => string;
  onCell: (members: [string, string | null][]) => void;
}) {
  const { palette } = useTheme();
  const map = useMemo(() => new Map(data.cells.map((c: CubeCell) => [key(c.r, c.c), c])), [data.cells]);
  const order = (dim: string) => sortTime(dim, data.members?.[dim] ?? [], (x) => x);
  const [r1, r2] = data.rows;
  const col = data.cols[0];
  const colMembers: (string | null)[] = col ? [...order(col), null] : [null];

  // Row lines: [members, kind]; kind "body" is shaded, "sub" / "all" are totals.
  type Line = { r: (string | null)[]; kind: "body" | "sub" | "all" };
  const lines: Line[] = [];
  if (!r1) lines.push({ r: [], kind: "all" });
  else if (!r2) {
    for (const m of order(r1)) lines.push({ r: [m], kind: "body" });
    lines.push({ r: [null], kind: "all" });
  } else {
    for (const m of order(r1)) {
      const inner = order(r2).filter((m2) => data.cells.some((c) => c.r[0] === m && c.r[1] === m2));
      for (const m2 of inner) lines.push({ r: [m, m2], kind: "body" });
      lines.push({ r: [m, null], kind: "sub" });
    }
    lines.push({ r: [null, null], kind: "all" });
  }

  const body = data.cells.filter((c) => c.value != null && c.r.every((m) => m != null) && (!col || c.c != null) && (data.rows.length > 0 || col));
  const vals = body.map((c) => c.value as number);
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const fmt = (v: number | null) => byKind(data.measure.format, v);

  if (!data.n) return <p className="py-6 text-center text-[13px] text-ink-2">No applications match.</p>;
  return (
    <div className="max-h-[560px] overflow-auto rounded-lg border border-line">
      <table className="num w-full border-collapse text-[13px]">
        <thead className="sticky top-0 z-10 bg-surface">
          <tr className="text-[12px] text-ink-2">
            {data.rows.length === 0 && <th className="px-3 py-2 text-left font-semibold" />}
            {data.rows.map((d) => <th key={d} className="whitespace-nowrap px-3 py-2 text-left font-semibold">{dimLabel(d)}</th>)}
            {colMembers.map((m) => (
              <th key={m ?? "__all"} className={clsx("whitespace-nowrap px-3 py-2 text-right font-semibold", m == null && "border-l border-line text-ink")}>
                {m == null ? (col ? "All" : data.measure.label) : label(m)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {lines.map((ln, i) => (
            <tr key={i} className={clsx(ln.kind !== "body" && "bg-raised/50 font-semibold", ln.kind === "all" && "border-t-2 border-line")}>
              {data.rows.length === 0 && <th className="px-3 py-2 text-left font-semibold text-ink">All</th>}
              {data.rows.map((d, j) => {
                const m = ln.r[j];
                // Repeat the outer member only on its first line; subtotal lines read "All".
                const firstOfGroup = j === 0 && data.rows.length === 2 && i > 0 && lines[i - 1].r[0] === m && ln.kind === "body";
                return (
                  <th key={d} scope="row" className="whitespace-nowrap px-3 py-2 text-left font-medium text-ink">
                    {m == null ? (j === 0 || ln.kind === "sub" ? "All" : "") : firstOfGroup ? "" : label(m)}
                  </th>
                );
              })}
              {colMembers.map((cm) => {
                const cell = map.get(key(ln.r, cm));
                const total = ln.kind !== "body" || cm == null && !!col;
                const shade = !total && cell?.value != null && hi > lo ? seqColor(palette, 0.05 + ((cell.value - lo) / (hi - lo)) * 0.75) : null;
                const members: [string, string | null][] = [...data.rows.map((d, j) => [d, ln.r[j] ?? null] as [string, string | null]), ...(col ? [[col, cm] as [string, string | null]] : [])];
                return (
                  <td key={cm ?? "__all"} className={clsx("p-0 text-right", cm == null && col && "border-l border-line")}
                    style={shade ? { background: shade.fill, color: shade.text } : undefined}>
                    {cell ? (
                      <button type="button" className="w-full px-3 py-2 text-right hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
                        title={`${num(cell.n)} applications · open in Explorer`} onClick={() => onCell(members)}>
                        {fmt(cell.value)}
                      </button>
                    ) : <span className="block px-3 py-2 text-ink-3">·</span>}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---- panel charts --------------------------------------------------------------------------------------

const GRADE_DIMS = new Set(["final_grade", "score_grade", "rule_grade"]);

/** Colour per member: decision tokens for decisions, the grade ramp for grades, else the categorical palette. */
function useMemberColor() {
  const { palette } = useTheme();
  const grades = useGradeColors();
  return (dim: string, member: string, i: number): string => {
    if (dim === "decision") {
      const m = member.toLowerCase();
      return m.startsWith("approve") ? palette.status.good : m.includes("review") ? palette.status.warning : m.startsWith("reject") ? palette.status.critical : palette.muted;
    }
    if (GRADE_DIMS.has(dim) && member in grades) return grades[member as keyof typeof grades].fill;
    if (member === "No rule hit" || member === "(missing)") return palette.muted;
    return palette.series[i % palette.series.length];
  };
}

/** Rows x series table for one time / segment dimension (row totals and column totals left out). */
function seriesRows(data: CubeResponse, dim: string, col: string | null) {
  const xs = sortTime(dim, data.members?.[dim] ?? [], (x) => x);
  const series = col ? sortTime(col, data.members?.[col] ?? [], (x) => x) : ["value"];
  const at = new Map(data.cells.filter((c) => c.r.length === 1 && c.r[0] != null).map((c) => [`${c.r[0]}|${col ? c.c : "value"}`, c]));
  const rows = xs.map((m) => {
    const o: Record<string, string | number | null> = { member: m };
    for (const s of series) o[s] = col && at.get(`${m}|${s}`) == null ? 0 : at.get(`${m}|${s}`)?.value ?? null;
    return o;
  });
  return { rows, series };
}

/** Grand-total value (r all null, c null): the filtered cohort's overall figure. */
const grandTotal = (data: CubeResponse) => data.cells.find((c) => c.r.every((m) => m == null) && c.c == null)?.value ?? null;

function useAxes(format: string, pct100 = false) {
  const { palette } = useTheme();
  const tick = { fill: palette.muted, fontSize: 11 };
  return {
    palette,
    tick,
    yFmt: pct100 ? (v: number) => `${Math.round(v * 100)}%` : (v: number) => byKind(format, v),
  };
}

/** Stacked bars per period split by a dimension (100% when `pct`); clicking a segment drills through. */
function TimeStack({ data, dim, split, pct: pct100, onPick }: {
  data: CubeResponse; dim: string; split: string; pct: boolean; onPick: (m: [string, string | null][]) => void;
}) {
  const color = useMemberColor();
  const { palette, tick, yFmt } = useAxes(data.measure.format, pct100);
  const { rows, series } = seriesRows(data, dim, split);
  if (!rows.length) return <p className="py-6 text-center text-[13px] text-ink-2">No applications match.</p>;
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={rows} margin={{ top: 8, right: 12, bottom: 4, left: 0 }} stackOffset={pct100 ? "expand" : undefined} barCategoryGap="12%">
        <CartesianGrid vertical={false} stroke={palette.grid} />
        <XAxis dataKey="member" tick={tick} axisLine={{ stroke: palette.axis }} tickLine={false} minTickGap={14} />
        <YAxis tickFormatter={yFmt} tick={tick} axisLine={false} tickLine={false} width={52} />
        <Tooltip cursor={{ fill: palette.grid, opacity: 0.4 }} content={({ active, payload, label: l }) => {
          if (!active || !payload?.length) return null;
          const total = payload.reduce((s2, p) => s2 + (Number(p.value) || 0), 0);
          return (
            <TooltipCard title={label(String(l))} rows={[...payload].reverse().map((p) => [
              <span key={String(p.dataKey)} className="inline-flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-sm" style={{ background: p.color }} />{label(String(p.dataKey))}
              </span>,
              pct100 && total ? `${num(Number(p.value))} (${Math.round((Number(p.value) / total) * 100)}%)` : num(Number(p.value)),
            ] as [ReactNode, string])} />
          );
        }} />
        <Legend iconType="circle" iconSize={8} formatter={(v: string) => label(v)} wrapperStyle={{ fontSize: 12, color: palette.ink2, paddingTop: 6 }} />
        {series.map((s2, i) => (
          <Bar key={s2} dataKey={s2} stackId="a" fill={color(split, s2, i)} isAnimationActive={false} style={{ cursor: "pointer" }}
            onClick={(d: { payload?: { member?: string } }) => d?.payload?.member && onPick([[dim, d.payload.member], [split, s2]])} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Default rate per period (null periods skipped) with the cohort's overall rate as a dashed reference. */
function RateLine({ data, dim, onPick }: { data: CubeResponse; dim: string; onPick: (m: [string, string | null][]) => void }) {
  const { palette, tick, yFmt } = useAxes(data.measure.format);
  const { rows } = seriesRows(data, dim, null);
  const pts = rows.filter((r) => r.value != null);
  const overall = grandTotal(data);
  if (!pts.length) return <p className="py-6 text-center text-[13px] text-ink-2">No known outcomes.</p>;
  return (
    <ResponsiveContainer width="100%" height={240}>
      <LineChart data={pts} margin={{ top: 8, right: 12, bottom: 4, left: 0 }} style={{ cursor: "pointer" }}
        onClick={(e: { activeLabel?: string | number } | null) => { if (e?.activeLabel != null) onPick([[dim, String(e.activeLabel)]]); }}>
        <CartesianGrid vertical={false} stroke={palette.grid} />
        <XAxis dataKey="member" tick={tick} axisLine={{ stroke: palette.axis }} tickLine={false} minTickGap={14} />
        <YAxis tickFormatter={yFmt} tick={tick} axisLine={false} tickLine={false} width={52} />
        <Tooltip cursor={{ stroke: palette.axis }} content={({ active, payload, label: l }) => active && payload?.length
          ? <TooltipCard title={label(String(l))} rows={[[data.measure.label, yFmt(Number(payload[0].value))]]} /> : null} />
        {overall != null && <ReferenceLine y={overall} stroke={palette.ink2} strokeDasharray="4 4" strokeWidth={1}
          label={{ value: `All ${yFmt(overall)}`, position: "insideTopRight", fill: palette.ink2, fontSize: 11 }} />}
        <Line type="linear" dataKey="value" stroke={palette.series[0]} strokeWidth={2} dot={{ r: 2.5, fill: palette.series[0] }}
          activeDot={{ r: 5 }} isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}

/** Horizontal default-rate bars per member in the dimension's natural order, with the overall rate dashed. */
function SegmentBars({ data, dim, onPick }: { data: CubeResponse; dim: string; onPick: (m: [string, string | null][]) => void }) {
  const color = useMemberColor();
  const { palette, tick, yFmt } = useAxes(data.measure.format);
  const { rows } = seriesRows(data, dim, null);
  const overall = grandTotal(data);
  if (!rows.length) return <p className="py-6 text-center text-[13px] text-ink-2">No applications match.</p>;
  return (
    <ResponsiveContainer width="100%" height={Math.max(180, rows.length * 30 + 40)}>
      <BarChart data={rows} layout="vertical" margin={{ top: 16, right: 48, bottom: 4, left: 4 }}>
        <CartesianGrid horizontal={false} stroke={palette.grid} />
        <XAxis type="number" tickFormatter={yFmt} tick={tick} axisLine={{ stroke: palette.axis }} tickLine={false} />
        <YAxis type="category" dataKey="member" width={170} tickFormatter={label} tick={{ ...tick, fill: palette.ink2 }} axisLine={false} tickLine={false} interval={0} />
        <Tooltip cursor={{ fill: palette.grid, opacity: 0.4 }} content={({ active, payload, label: l }) => active && payload?.length
          ? <TooltipCard title={label(String(l))} rows={[[data.measure.label, payload[0].value == null ? "–" : yFmt(Number(payload[0].value))]]} /> : null} />
        {overall != null && <ReferenceLine x={overall} stroke={palette.ink2} strokeDasharray="4 4" strokeWidth={1}
          label={{ value: `All ${yFmt(overall)}`, position: "top", fill: palette.ink2, fontSize: 11 }} />}
        <Bar dataKey="value" maxBarSize={20} radius={[0, 4, 4, 0]} isAnimationActive={false} style={{ cursor: "pointer" }}
          label={{ position: "right", formatter: (v: number) => (v == null ? "" : yFmt(v)), fill: palette.ink2, fontSize: 11 }}
          onClick={(d: { payload?: { member?: string } }) => d?.payload?.member && onPick([[dim, d.payload.member]])}>
          {rows.map((r, i) => <Cell key={String(r.member)} fill={color(dim, String(r.member), i)} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// ---- SQL queries tab (the saved OLAP analyses in src/sql/olap) -----------------------------------------------

interface OlapItem { name: string; sql: string }
interface OlapResult { name: string; sql: string; columns: string[]; rows: Record<string, unknown>[] }

/** Saved analyses: [title, OLAP technique]. */
const TITLES: Record<string, [string, string]> = {
  "01": ["Roll-up", "Age → income bracket → all"],
  "02": ["Cube", "Contract type × gender with every subtotal"],
  "03": ["Drill-down", "Inside age 25–34: employment type → occupation"],
  "04": ["Slice, dice & pivot", "Cash loans with bureau history; days-late bands as columns"],
  "05": ["Time roll-up", "Bureau credits by quarter → year → all"],
  "06": ["Ranking", "Top 3 riskiest occupations per age group"],
};

function SqlBlock({ sql }: { sql: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="relative mt-4 overflow-hidden rounded-lg border border-line bg-raised/50">
      <button className="absolute right-2 top-2 rounded-md p-1.5 text-ink-3 hover:bg-surface hover:text-ink" aria-label="Copy SQL"
        onClick={() => navigator.clipboard?.writeText(sql).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1200); })}>
        {copied ? <Check className="h-3.5 w-3.5 text-good" /> : <Copy className="h-3.5 w-3.5" />}
      </button>
      <pre className="max-h-80 overflow-auto p-4 pr-10 font-mono text-[12px] leading-relaxed text-ink-2">{sql.trim()}</pre>
    </div>
  );
}

function SavedQueries() {
  const list = useApi<OlapItem[]>("/api/olap");
  const [name, setName] = useState<string | null>(null);
  const current = name ?? list.data?.[0]?.name ?? null;
  const result = useApi<OlapResult>(current ? `/api/olap/${encodeURIComponent(current)}` : null);
  const [showSql, setShowSql] = useState(false);

  return (
    <div className="grid gap-4 lg:grid-cols-[280px_1fr]">
      <Card title="Saved analyses" bodyClassName="px-2 pb-2">
        <ul className="space-y-0.5">
          {(list.data ?? []).map((q) => {
            const [title, sub] = TITLES[q.name.slice(0, 2)] ?? [q.name, ""];
            return (
              <li key={q.name}>
                <button onClick={() => setName(q.name)}
                  className={clsx("w-full rounded-lg px-3 py-2 text-left transition", current === q.name ? "bg-accent-soft" : "hover:bg-raised")}>
                  <span className={clsx("flex items-center gap-2 text-[13px] font-medium", current === q.name ? "text-accent-ink" : "text-ink")}>
                    <Play className="h-3 w-3" /> {title}
                  </span>
                  <span className="mt-0.5 block text-xs leading-snug text-ink-3">{sub}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </Card>
      <Card title={current ? (TITLES[current.slice(0, 2)]?.[0] ?? current) : "…"}
        actions={
          <>
            <button className={clsx("btn text-xs", showSql && "bg-raised text-ink")} onClick={() => setShowSql((s) => !s)}
              aria-pressed={showSql} disabled={!result.data}>
              <Code2 className="h-3.5 w-3.5" /> {showSql ? "Hide SQL" : "Show SQL"}
            </button>
            <Table2 className="h-4 w-4 text-ink-3" aria-hidden />
          </>
        }>
        {result.data ? (
          <>
            <div className="max-h-[460px] overflow-auto rounded-lg border border-line">
              {result.data.rows.length === 0 ? (
                <p className="p-6 text-center text-sm text-ink-3">No rows.</p>
              ) : (
                <table className="table num">
                  <thead className="sticky top-0 bg-surface">
                    <tr>{result.data.columns.map((c) => <th key={c}>{c}</th>)}</tr>
                  </thead>
                  <tbody>
                    {result.data.rows.map((r, i) => {
                      const isTotal = Object.values(r).some((v) => v === "ALL");
                      return (
                        <tr key={i} className={clsx(isTotal && "bg-raised/50 font-semibold")}>
                          {result.data!.columns.map((c) => (
                            <td key={c} className="text-ink-2">
                              {typeof r[c] === "number" ? num(r[c] as number, Number.isInteger(r[c]) ? 0 : 2) : String(r[c] ?? "–")}
                            </td>
                          ))}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
            {showSql && <SqlBlock sql={result.data.sql} />}
          </>
        ) : result.error ? <ErrorBox message={result.error} /> : <ChartSkeleton height={260} />}
      </Card>
    </div>
  );
}
