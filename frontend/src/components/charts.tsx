import { useMemo } from "react";
import {
  Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import type { DimRow, Grade } from "../lib/api";
import { fixed, label, num, pct } from "../lib/format";
import { useTheme, type ThemeName } from "../lib/theme";
import { TooltipCard } from "./ui";

/** Groups below this many labelled loans are drawn faded: their rate is mostly noise. */
export const MIN_RELIABLE = 300;
/** Groups below this are not drawn at all (a rate over a handful of loans is meaningless); see tooltip/table. */
export const MIN_SHOWN = 30;

const axisTick = (fill: string) => ({ fill, fontSize: 12 });

/** Clean rate ticks (0 / 5% / 10% ...) covering max, plus the matching domain. */
export function rateTicks(max: number): { ticks: number[]; domain: [number, number] } {
  const target = Math.max(max, 0.001) / 4;
  const steps = [0.005, 0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25];
  const step = steps.find((s) => s >= target) ?? 0.25;
  const top = Math.ceil((max * 1.05) / step) * step;
  const ticks: number[] = [];
  for (let v = 0; v <= top + 1e-9; v += step) ticks.push(Number(v.toFixed(4)));
  return { ticks, domain: [0, top] };
}

const tickPct = (v: number) => pct(v, v !== 0 && v < 0.1 && Math.round(v * 1000) % 10 !== 0 ? 1 : 0);

/**
 * Default rate per member of one dimension, single hue (one series -> slot 1), portfolio average as
 * a reference line. Columns for short ordered labels, horizontal bars for long nominal ones.
 */
export function RateBars({ data, avg, layout = "columns", height = 300, onSelect, selected = [] }: {
  data: DimRow[];
  avg?: number | null;
  layout?: "columns" | "bars";
  height?: number;
  onSelect?: (member: string) => void;
  selected?: string[];
}) {
  const { palette } = useTheme();
  const rows = useMemo(() => data.filter((d) => d.default_rate !== null && d.n_labelled >= MIN_SHOWN), [data]);
  const scale = rateTicks(Math.max(avg ?? 0, ...rows.map((d) => d.default_rate ?? 0)));
  const fill = (d: DimRow) =>
    selected.length && !selected.includes(d.member) ? palette.grid : palette.series[0];
  const opacity = (d: DimRow) => (d.n_labelled < MIN_RELIABLE ? 0.35 : 1);

  const tooltip = (
    <Tooltip
      cursor={{ fill: palette.grid, opacity: 0.45 }}
      content={({ active, payload }) => {
        if (!active || !payload?.length) return null;
        const d = payload[0].payload as DimRow;
        return (
          <TooltipCard
            title={label(d.member)}
            rows={[
              ["Default rate", pct(d.default_rate, 2)],
              ["Historical loans", num(d.n_labelled)],
              ["Applications", num(d.n_applications)],
              ...(d.n_labelled < MIN_RELIABLE ? [["Note", "small sample"] as [string, string]] : []),
            ]}
          />
        );
      }}
    />
  );

  const common = {
    data: rows,
    onClick: onSelect
      ? (e: { activePayload?: { payload: DimRow }[] }) => {
          const m = e?.activePayload?.[0]?.payload?.member;
          if (m) onSelect(m);
        }
      : undefined,
    style: { cursor: onSelect ? "pointer" : "default" },
  };

  if (layout === "bars") {
    const h = Math.max(height, rows.length * 34 + 40);
    return (
      <>
      <ResponsiveContainer width="100%" height={h}>
        <BarChart {...common} layout="vertical" margin={{ top: 4, right: 48, bottom: 4, left: 4 }}>
          <CartesianGrid horizontal={false} stroke={palette.grid} />
          <XAxis type="number" ticks={scale.ticks} domain={scale.domain} tickFormatter={tickPct}
            tick={axisTick(palette.muted)} axisLine={{ stroke: palette.axis }} tickLine={false} />
          <YAxis type="category" dataKey="member" width={150} tickFormatter={label}
            tick={axisTick(palette.ink2)} axisLine={false} tickLine={false} interval={0} />
          {tooltip}
          {avg != null && <ReferenceLine x={avg} stroke={palette.ink2} strokeWidth={1}
            label={{ value: `avg ${pct(avg)}`, position: "top", fill: palette.ink2, fontSize: 11 }} />}
          <Bar dataKey="default_rate" maxBarSize={20} radius={[0, 4, 4, 0]} isAnimationActive={false}
            label={{ position: "right", formatter: (v: number) => pct(v), fill: palette.ink2, fontSize: 11 }}>
            {rows.map((d) => <Cell key={d.member} fill={fill(d)} fillOpacity={opacity(d)} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
      
      </>
    );
  }

  return (
    <>
    <ResponsiveContainer width="100%" height={height}>
      <BarChart {...common} margin={{ top: 20, right: avg != null ? 64 : 8, bottom: 4, left: 0 }}>
        <CartesianGrid vertical={false} stroke={palette.grid} />
        <XAxis dataKey="member" tickFormatter={label} tick={axisTick(palette.ink2)}
          axisLine={{ stroke: palette.axis }} tickLine={false} interval={0} />
        <YAxis ticks={scale.ticks} domain={scale.domain} tickFormatter={tickPct} tick={axisTick(palette.muted)}
          axisLine={false} tickLine={false} width={44} />
        {tooltip}
        {avg != null && <ReferenceLine y={avg} stroke={palette.ink2} strokeWidth={1} ifOverflow="extendDomain"
          label={{ value: `avg ${pct(avg)}`, position: "right", fill: palette.ink2, fontSize: 11 }} />}
        <Bar dataKey="default_rate" maxBarSize={24} radius={[4, 4, 0, 0]} isAnimationActive={false}
          label={{ position: "top", formatter: (v: number) => pct(v), fill: palette.ink2, fontSize: 11 }}>
          {rows.map((d) => <Cell key={d.member} fill={fill(d)} fillOpacity={opacity(d)} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
    
    </>
  );
}

// ---- DSS: A-E grade tokens ---------------------------------------------------------------------

/**
 * Sequential risk ramp A -> E: green -> yellow-green -> amber -> orange -> red, so higher risk looks redder.
 * Light theme: deep fills with white letters; dark theme: bright fills with dark letters. Every fill is
 * >= 5:1 against the card and every letter >= 5:1 against its fill (WCAG), and the letter is always
 * printed, so colour is never the only cue. Decision pills keep their own colour + icon + word.
 */
const GRADE_TOKENS: Record<ThemeName, Record<Grade, { fill: string; text: string }>> = {
  light: {
    A: { fill: "#1e7a3a", text: "#ffffff" },
    B: { fill: "#557a12", text: "#ffffff" },
    C: { fill: "#946300", text: "#ffffff" },
    D: { fill: "#b54708", text: "#ffffff" },
    E: { fill: "#b3261e", text: "#ffffff" },
  },
  dark: {
    A: { fill: "#7fd69b", text: "#10181c" },
    B: { fill: "#b8d35a", text: "#10181c" },
    C: { fill: "#f2c14e", text: "#10181c" },
    D: { fill: "#f59e5b", text: "#10181c" },
    E: { fill: "#ff8a80", text: "#10181c" },
  },
};

/** Fallback wording; /api/grades returns the configured labels (dss.grade_labels). */
export const GRADE_LABELS: Record<Grade, string> = {
  A: "Very low risk", B: "Low risk", C: "Moderate risk", D: "High risk", E: "Very high risk",
};

export function useGradeColors(): Record<Grade, { fill: string; text: string }> {
  const { theme } = useTheme();
  return GRADE_TOKENS[theme];
}

/** Letter chip in the grade colour; `null` renders a neutral "none" chip (no rule violated). */
export function GradeChip({ grade, size = "md", title }: { grade: Grade | null; size?: "sm" | "md" | "lg"; title?: string }) {
  const colors = useGradeColors();
  const { palette } = useTheme();
  const c = grade ? colors[grade] : { fill: "transparent", text: palette.ink2 };
  const dims = size === "lg" ? "h-11 w-11 text-2xl" : size === "sm" ? "h-6 w-6 text-xs" : "h-8 w-8 text-base";
  return (
    <span
      className={`inline-grid shrink-0 place-items-center rounded-[10px] font-extrabold leading-none ${dims} ${grade ? "" : "border border-dashed border-line"}`}
      style={{ background: c.fill, color: c.text }}
      title={title ?? (grade ? `Grade ${grade}: ${GRADE_LABELS[grade]}` : "No grade")}
      aria-label={grade ? `Grade ${grade}` : "No grade"}
    >
      {grade ?? "–"}
    </span>
  );
}

/** Global importance: mean |SHAP| per feature, one hue, horizontal bars (labels are long). */
export function ImportanceBars({ data, height = 300 }: {
  data: { feature: string; label: string; mean_abs_shap: number }[];
  height?: number;
}) {
  const { palette } = useTheme();
  const rows = data.map((d) => ({ ...d, name: d.label }));
  return (
    <ResponsiveContainer width="100%" height={Math.max(height, rows.length * 28 + 30)}>
      <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 48, bottom: 4, left: 4 }}>
        <CartesianGrid horizontal={false} stroke={palette.grid} />
        <XAxis type="number" tickFormatter={(v: number) => fixed(v, 2)} tick={axisTick(palette.muted)}
          axisLine={{ stroke: palette.axis }} tickLine={false} />
        <YAxis type="category" dataKey="name" width={200} tick={axisTick(palette.ink2)} axisLine={false} tickLine={false} interval={0} />
        <Tooltip
          cursor={{ fill: palette.grid, opacity: 0.45 }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null;
            const d = payload[0].payload as (typeof rows)[number];
            return <TooltipCard title={d.label} rows={[["Column", d.feature], ["Mean |SHAP|", fixed(d.mean_abs_shap, 3)]]} />;
          }}
        />
        <Bar dataKey="mean_abs_shap" fill={palette.series[0]} maxBarSize={18} radius={[0, 4, 4, 0]} isAnimationActive={false}
          label={{ position: "right", formatter: (v: number) => fixed(v, 2), fill: palette.ink2, fontSize: 11 }} />
      </BarChart>
    </ResponsiveContainer>
  );
}

/**
 * PD vs actual default per decile of PD: two lines on one percent axis (mean PD, actual default rate).
 * Close lines = the PD can be read as a probability. Legend + tooltip name both series.
 */
export function CalibrationChart({ data, height = 280 }: {
  data: { bin: number; n: number; mean_pd: number; default_rate: number }[];
  height?: number;
}) {
  const { palette } = useTheme();
  if (!data.length) return <p className="grid h-40 place-items-center text-sm text-ink-2">No calibration data yet.</p>;
  const scale = rateTicks(Math.max(...data.map((d) => Math.max(d.mean_pd, d.default_rate))));
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 12, right: 16, bottom: 16, left: 0 }}>
        <CartesianGrid vertical={false} stroke={palette.grid} />
        <XAxis dataKey="bin" tick={axisTick(palette.muted)} axisLine={{ stroke: palette.axis }} tickLine={false}
          label={{ value: "PD decile (1 = safest)", position: "insideBottom", offset: -8, fill: palette.muted, fontSize: 12 }} />
        <YAxis ticks={scale.ticks} domain={scale.domain} tickFormatter={tickPct} tick={axisTick(palette.muted)}
          axisLine={false} tickLine={false} width={44} />
        <Tooltip
          cursor={{ stroke: palette.axis }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null;
            const d = payload[0].payload as (typeof data)[number];
            return (
              <TooltipCard title={`Decile ${d.bin}`}
                rows={[["Mean PD", pct(d.mean_pd, 2)], ["Actual default rate", pct(d.default_rate, 2)], ["Historical loans", num(d.n)]]} />
            );
          }}
        />
        <Legend iconType="plainline" wrapperStyle={{ fontSize: 12, color: palette.ink2, paddingTop: 12 }} />
        <Line type="linear" dataKey="mean_pd" name="Mean PD" stroke={palette.series[0]} strokeWidth={2}
          dot={{ r: 4, fill: palette.series[0], stroke: palette.surface, strokeWidth: 2 }} isAnimationActive={false} />
        <Line type="linear" dataKey="default_rate" name="Actual default rate" stroke={palette.series[1]} strokeWidth={2}
          strokeDasharray="5 3" dot={{ r: 4, fill: palette.series[1], stroke: palette.surface, strokeWidth: 2 }} isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}
