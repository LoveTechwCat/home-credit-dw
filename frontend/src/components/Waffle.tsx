import clsx from "clsx";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import { useNavigate } from "react-router-dom";
import type { CohortCondition, CohortMetrics } from "../lib/api";
import { exploreHref } from "../lib/cohort";
import { compact, num, pct } from "../lib/format";
import { useTheme } from "../lib/theme";
import { DssError } from "./AssessmentView";

type Key = "approve" | "review" | "reject";

/** Decision value used by the Explorer's "decision" field. */
const DECISION_VALUE: Record<Key, string> = { approve: "Approve", review: "Manual review", reject: "Reject" };
const GROUP_LABEL: Record<Key, string> = { approve: "Approved", review: "Manual review", reject: "Rejected" };
const ORDER: Key[] = ["approve", "review", "reject"];

/** Calm teal / blue / slate per decision (not the traffic-light status colours), per theme. */
const COLORS: Record<"light" | "dark", Record<Key, string>> = {
  light: { approve: "#00756f", review: "#1f6fb2", reject: "#647880" },
  dark: { approve: "#3cc4b6", review: "#6fb2f0", reject: "#869ba1" },
};

/** Integers that sum exactly to `total`, proportional to `weights` (largest-remainder method). */
export function largestRemainder(weights: number[], total: number): number[] {
  const sum = weights.reduce((a, b) => a + b, 0);
  if (!sum || total <= 0) return weights.map(() => 0);
  const raw = weights.map((w) => (w / sum) * total);
  const out = raw.map(Math.floor);
  let left = total - out.reduce((a, b) => a + b, 0);
  raw.map((r, i) => ({ i, rem: r - Math.floor(r) }))
    .sort((a, b) => b.rem - a.rem)
    .forEach(({ i }) => { if (left > 0) { out[i] += 1; left -= 1; } });
  return out;
}

interface Group { key: Key; good: number; bad: number; squares: number; badSquares: number }

function buildGroups(o: CohortMetrics["outcome_by_decision"]): Group[] {
  const totals = ORDER.map((k) => o[k].good + o[k].bad);
  const squares = largestRemainder(totals, 100);
  return ORDER.map((k, i) => {
    // Bad squares inside each group: same method on the group's own square count.
    const [, badSquares] = largestRemainder([o[k].good, o[k].bad], squares[i]);
    return { key: k, good: o[k].good, bad: o[k].bad, squares: squares[i], badSquares };
  });
}

const ROWS = 4;
const COLS = 25;
/** Fill sweep across the 25 columns, then the defaulted dots pulse in. */
const FILL_MS = 1000;
const COL_STEP_MS = FILL_MS / COLS;

/** The entrance plays once per page load: navigating back, refetching or switching theme shows the final state. */
let entrancePlayed = false;

const prefersReducedMotion = () =>
  typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/** Count from 0 to `target` with an ease-out over `ms` (requestAnimationFrame); instant when `animate` is off. */
function useCountUp(target: number, animate: boolean, ms = FILL_MS): { v: number; done: boolean } {
  const [state, setState] = useState({ v: animate ? 0 : target, done: !animate });
  useEffect(() => {
    if (!animate) { setState({ v: target, done: true }); return; }
    let raf = 0;
    const t0 = performance.now();
    const step = (t: number) => {
      const p = Math.min(1, (t - t0) / ms);
      setState({ v: Math.round(target * (1 - Math.pow(1 - p, 3))), done: p >= 1 });
      if (p < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [target, animate, ms]);
  return state;
}

/**
 * "Out of every 100 applications" card: historical loans by decision as a 4×25 waffle (10×10 on phones),
 * defaulted loans marked with a dot at the end of each group, a label over each block and two plain stats.
 * Clicking a square or a group label opens the Explorer on that cohort.
 */
export function WaffleCard({ o, error, status }: {
  o?: CohortMetrics["outcome_by_decision"];
  error?: string;
  status?: number;
}) {
  const navigate = useNavigate();
  const defaultedKey = (
    <button type="button" onClick={() => navigate(exploreHref([{ field: "outcome", op: "is", value: "Defaulted" }]))}
      className="inline-flex items-center gap-1.5 rounded-md text-[12.5px] text-ink-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50">
      <span className="h-2 w-2 rounded-full bg-bad" aria-hidden /> Defaulted
    </button>
  );
  return (
    <section className="card card-pad" aria-labelledby="waffle-title">
      <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 id="waffle-title" className="text-[19px] font-extrabold tracking-[-0.01em] text-ink">
          Out of every 100 applications <span className="ml-2 text-[13px] font-normal text-ink-2">Historical loans</span>
        </h2>
        {o && defaultedKey}
      </header>
      <div className="mt-4">
        {o ? <DecisionWaffle o={o} /> : error ? <DssError status={status} message={error} /> : <WaffleSkeleton />}
      </div>
    </section>
  );
}

const GAP = 5;
const LABEL_GAP = 12;
const LABEL_HEIGHT = 58;

/** x of a block's first column inside a grid of `width` px (25 equal columns, 5px gaps). */
const blockX = (col: number, width: number) => col * ((width - GAP * (COLS - 1)) / COLS + GAP);

const labelDetail = (g: Group) => {
  const n = g.good + g.bad;
  return `${compact(n)} · ${pct(n ? g.bad / n : null, 1)} defaulted`;
};

let measureCtx: CanvasRenderingContext2D | null = null;
/** Text width with the page's font (canvas, so nothing is rendered twice in the DOM). */
function textWidth(text: string, font: string): number {
  measureCtx ??= document.createElement("canvas").getContext("2d");
  if (!measureCtx) return text.length * 8;
  measureCtx.font = font;
  return measureCtx.measureText(text).width;
}
const FAMILY = '"Be Vietnam Pro", system-ui, sans-serif';
/** Widest of the label's three lines (+ a little slack for rounding and the focus ring). */
function labelWidth(g: Group): number {
  const line1 = 10 + 6 + textWidth(`${g.squares}%`, `800 20px ${FAMILY}`);
  const line2 = textWidth(GROUP_LABEL[g.key], `600 13px ${FAMILY}`);
  const line3 = textWidth(labelDetail(g), `400 12.5px ${FAMILY}`);
  return Math.ceil(Math.max(line1, line2, line3)) + 4;
}

/**
 * Left positions for the labels: each starts at its block, never before the previous label's end + 12px,
 * the last one is right-aligned to the grid edge. Returns null when they cannot fit (use the legend row).
 */
export function layoutLabels(widths: number[], starts: number[], width: number): number[] | null {
  const xs: number[] = [];
  let prevEnd = -Infinity;
  for (let i = 0; i < widths.length; i++) {
    const last = i === widths.length - 1;
    const want = last ? width - widths[i] : starts[i];
    const x = Math.max(want, prevEnd + LABEL_GAP, 0);
    if (x + widths[i] > width + 0.5 || (last && x > want + 0.5)) return null;
    xs.push(x);
    prevEnd = x + widths[i];
  }
  return xs;
}

function useElementWidth(ref: RefObject<HTMLElement>): number {
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.getBoundingClientRect().width);
    const ro = new ResizeObserver(([e]) => setW(e.contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return w;
}

/** Flips once the web font is ready, so label widths are re-measured with the real font. */
function useFontsReady(): boolean {
  const [ready, setReady] = useState(() => typeof document === "undefined" || !("fonts" in document) || document.fonts.status === "loaded");
  useEffect(() => {
    if (ready || !("fonts" in document)) return;
    let live = true;
    document.fonts.ready.then(() => { if (live) setReady(true); });
    return () => { live = false; };
  }, [ready]);
  return ready;
}

function DecisionWaffle({ o }: { o: CohortMetrics["outcome_by_decision"] }) {
  const navigate = useNavigate();
  const { theme } = useTheme();
  const colors = COLORS[theme];
  const groups = useMemo(() => buildGroups(o), [o]);
  const total = groups.reduce((s, g) => s + g.good + g.bad, 0);
  // Decided once at mount: play the entrance only the first time on this page load and without reduced motion.
  const [animate] = useState(() => !entrancePlayed && !prefersReducedMotion());
  useEffect(() => { entrancePlayed = true; }, []);
  // Hovering a block / label dims the other blocks; hovering a defaulted square highlights every defaulted one.
  const [hover, setHover] = useState<Key | "bad" | null>(null);

  // Each group's good squares first, then its defaulted ones; the wide grid flows column by column, so a
  // group is one block left → right with its defaulted squares at the bottom of its last column(s).
  const cells = groups.flatMap((g) => [
    ...Array.from({ length: g.squares - g.badSquares }, () => ({ g, bad: false })),
    ...Array.from({ length: g.badSquares }, () => ({ g, bad: true })),
  ]);
  let start = 0;
  const startCols = groups.map((g) => {
    const c = Math.floor(start / ROWS);
    start += g.squares;
    return c;
  });
  const wideRef = useRef<HTMLDivElement>(null);
  const width = useElementWidth(wideRef);
  const fontsReady = useFontsReady();
  const positions = useMemo(
    () => (width ? layoutLabels(groups.map((g) => labelWidth(g)), startCols.map((c) => blockX(c, width)), width) : null),
    // startCols derive from groups; fontsReady re-measures once the web font has loaded.
    [groups, width, fontsReady],
  );

  const open = (g: Group, bad: boolean) => {
    const conds: CohortCondition[] = [{ field: "decision", op: "is", value: DECISION_VALUE[g.key] }];
    if (bad) conds.push({ field: "outcome", op: "is", value: "Defaulted" });
    navigate(exploreHref(conds));
  };
  const tip = (g: Group, bad?: boolean) => {
    const n = g.good + g.bad;
    const head = `${GROUP_LABEL[g.key]}: ${num(n)} loans (${pct(total ? n / total : null, 1)})`;
    if (bad === undefined) return `${head} · ${num(g.bad)} defaulted (${pct(n ? g.bad / n : null, 1)})`;
    return bad ? `${head} · defaulted: ${num(g.bad)} (${pct(n ? g.bad / n : null, 1)} of the group)`
      : `${head} · repaid: ${num(g.good)} (${pct(n ? g.good / n : null, 1)} of the group)`;
  };

  const summary = groups.map((g) => `${GROUP_LABEL[g.key]} ${g.squares} (${g.badSquares} defaulted)`).join(", ");

  // Position of each defaulted square among all defaulted squares (for the staggered dot pulse).
  const badRank = new Map<number, number>();
  cells.forEach((c, i) => { if (c.bad) badRank.set(i, badRank.size); });
  const dim = (c: (typeof cells)[number]) => hover !== null && (hover === "bad" ? !c.bad : c.g.key !== hover);
  // `wide`: delays follow the column-major sweep; phones (10 × 10, row-major) sweep row by row in the same time.
  const cell = (c: (typeof cells)[number], i: number, wide: boolean) => {
    const step = wide ? Math.floor(i / ROWS) : Math.floor(i / 10) * (COLS / 10);
    return (
      // Outer square: hover dimming (opacity transition). Inner square: the one-off entrance animation.
      <div key={i} className="aspect-square transition-opacity duration-150" style={{ opacity: dim(c) ? 0.35 : 1 }}
        onMouseEnter={() => setHover(c.bad ? "bad" : c.g.key)}>
        <div title={tip(c.g, c.bad)} onClick={() => open(c.g, c.bad)}
          className={clsx("grid h-full w-full cursor-pointer place-items-center rounded-[4px]", animate && "waffle-fill")}
          style={{
            // Defaulted squares: a lighter shade of the group colour plus a dot (shape, not colour alone).
            background: c.bad ? `color-mix(in srgb, ${colors[c.g.key]} 45%, transparent)` : colors[c.g.key],
            animationDelay: animate ? `${Math.round(step * COL_STEP_MS)}ms` : undefined,
          }}>
          {c.bad && (
            <span className={clsx("h-1.5 w-1.5 rounded-full bg-bad ring-2 ring-surface", animate && "waffle-dot-in")}
              style={animate ? { animationDelay: `${FILL_MS + (badRank.get(i) ?? 0) * 60}ms` } : undefined} aria-hidden />
          )}
        </div>
      </div>
    );
  };
  // Three short lines per label (big %, name, count + default share) so each label is about as wide as its name.
  const groupLabel = (g: Group) => {
    const n = g.good + g.bad;
    return (
      <button type="button" title={tip(g)} onClick={() => open(g, false)}
        aria-label={`${g.squares}% ${GROUP_LABEL[g.key]}, ${compact(n)} loans, ${pct(n ? g.bad / n : null, 1)} defaulted`}
        onMouseEnter={() => setHover(g.key)} onMouseLeave={() => setHover(null)} onFocus={() => setHover(g.key)} onBlur={() => setHover(null)}
        className="flex flex-col items-start whitespace-nowrap rounded-md text-left hover:[&_.lbl-name]:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50">
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-[3px]" style={{ background: colors[g.key] }} aria-hidden />
          <span className="big-num text-[20px] leading-none text-ink"><CountUp value={g.squares} animate={animate} />%</span>
        </span>
        <span className="lbl-name mt-1 text-[13px] font-semibold leading-tight text-ink">{GROUP_LABEL[g.key]}</span>
        <span className="num text-[12.5px] leading-tight text-ink-2">{labelDetail(g)}</span>
      </button>
    );
  };

  return (
    <div>
      <div ref={wideRef} className="min-w-0 max-w-[880px]">
        {/* Wide layout (sm and up): labels anchored over their blocks (never overlapping), then the 4 × 25 grid.
            When the labels cannot fit side by side they move to a legend row under the grid instead. */}
        <div className="hidden sm:block">
          {positions && (
            <div className="relative mb-2" style={{ height: LABEL_HEIGHT }}>
              {groups.map((g, i) => (
                <div key={g.key} className="absolute top-0" style={{ left: positions[i] }}>{groupLabel(g)}</div>
              ))}
            </div>
          )}
          <div role="img" aria-label={`Out of every 100 applications: ${summary}.`} className="grid gap-[5px]"
            onMouseLeave={() => setHover(null)}
            style={{ gridTemplateColumns: `repeat(${COLS}, minmax(0, 1fr))`, gridTemplateRows: `repeat(${ROWS}, auto)`, gridAutoFlow: "column" }}>
            {cells.map((c, i) => cell(c, i, true))}
          </div>
          {!positions && <div className="mt-3 flex flex-wrap gap-x-6 gap-y-3">{groups.map((g) => <span key={g.key}>{groupLabel(g)}</span>)}</div>}
        </div>

        {/* Phones: 10 × 10, labels underneath. */}
        <div className="sm:hidden">
          <div role="img" aria-label={`Out of every 100 applications: ${summary}.`} className="grid max-w-[300px] grid-cols-10 gap-1"
            onMouseLeave={() => setHover(null)}>
            {cells.map((c, i) => cell(c, i, false))}
          </div>
          <div className="mt-3 flex flex-wrap gap-x-6 gap-y-3">{groups.map((g) => <span key={g.key}>{groupLabel(g)}</span>)}</div>
        </div>
      </div>
    </div>
  );
}

/**
 * Number that counts up with the fill, rendered as ONE text node (copy/paste and screen readers see a single
 * number). It is hidden from assistive tech only while it is still counting.
 */
function CountUp({ value, animate }: { value: number; animate: boolean }) {
  const { v, done } = useCountUp(value, animate);
  return <span aria-hidden={done ? undefined : true}>{v}</span>;
}


/** Same footprint as the loaded waffle (4 × 25 wide, 10 × 10 on phones), so nothing shifts when data arrives. */
function WaffleSkeleton() {
  return (
    <div role="status" aria-label="Loading" className="max-w-[880px]">
      <div className="mb-2 animate-pulse rounded bg-raised" style={{ height: LABEL_HEIGHT, width: "50%" }} />
      <div className="hidden gap-[5px] sm:grid" style={{ gridTemplateColumns: `repeat(${COLS}, minmax(0, 1fr))` }}>
        {Array.from({ length: 100 }, (_, i) => <div key={i} className="aspect-square animate-pulse rounded-[4px] bg-raised" />)}
      </div>
      <div className="grid max-w-[300px] grid-cols-10 gap-1 sm:hidden">
        {Array.from({ length: 100 }, (_, i) => <div key={i} className="aspect-square animate-pulse rounded-[4px] bg-raised" />)}
      </div>
    </div>
  );
}
