import clsx from "clsx";
import { MousePointerClick } from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { DssError } from "../components/AssessmentView";
import { RateBars } from "../components/charts";
import { SEGMENT_DIMS, SegmentTable } from "../components/Segments";
import { Card, ChartSkeleton, ErrorBox, PageHeader, Select, StatTile } from "../components/ui";
import { exploreHref } from "../lib/cohort";
import { useApi, usePostApi, type CohortMetrics, type DimRow, type KpiSummary } from "../lib/api";
import { WaffleCard } from "../components/Waffle";
import { fixed, num, pct } from "../lib/format";

/** 184.2B - the unit the business reads loan-book totals in. */
const bn = (v: number | null | undefined) => (v == null ? "–" : `${fixed(v / 1e9, 1)}B`);


/**
 * Overview: unfiltered headline KPIs plus two panels. It deliberately calls the API without the
 * persisted global filters, so the numbers never depend on hidden state.
 */
export default function Overview() {
  const k = useApi<KpiSummary>("/api/kpis/summary");
  // Whole-book decision × outcome counts (empty cohort) for the waffle.
  const waffle = usePostApi<{ cohort: CohortMetrics; book: CohortMetrics }>("explore/metrics", { conditions: [] });
  const d = k.data;
  const loading = !d && !k.error;

  return (
    <div className="space-y-6">
      <PageHeader title="Overview" />

      <WaffleCard o={waffle.data?.book.outcome_by_decision} error={waffle.error} status={waffle.status} />

      <section className="space-y-3" aria-label="Headline KPIs">
        <p className="kicker">All applications · {d ? num(d.n_applications) : "…"}</p>
        {k.error && <DssError status={k.status} message={k.error} />}
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
          <StatTile label="Default rate" accent loading={loading} value={pct(d?.default_rate, 2)} />
          <StatTile label="Total credit" loading={loading} value={bn(d?.total_credit)} />
          <StatTile label="Credit at risk" loading={loading} value={bn(d?.expected_loss)} />
          <StatTile label="Approval rate" loading={loading} value={pct(d?.approval_rate, 2)} />
          <StatTile label="Manual review rate" loading={loading} value={pct(d?.referral_rate, 2)} />
        </div>
      </section>

      <SegmentAnalysis avg={d?.default_rate ?? null} />
    </div>
  );
}

const DEFAULT_DIM = "credit_history_status";

/**
 * Segment analysis with one shared "Group by" picker (?dim=). Data comes from /api/by/{dim} without filter
 * params, so Overview never depends on the persisted filters; clicking a bar opens that group in the Explorer.
 */
function SegmentAnalysis({ avg }: { avg: number | null }) {
  const [params, setParams] = useSearchParams();
  const raw = params.get("dim");
  const dim = SEGMENT_DIMS.some((x) => x.value === raw) ? (raw as string) : DEFAULT_DIM;
  const dimLabel = SEGMENT_DIMS.find((x) => x.value === dim)!.label;
  const rows = useApi<DimRow[]>(`/api/by/${encodeURIComponent(dim)}`);
  const navigate = useNavigate();
  // Open the Explorer with this group as its cohort (no global filter is set).
  const openInExplorer = (member: string) => navigate(exploreHref([{ field: dim, op: "is", value: member }]));

  return (
    <section className="space-y-4" aria-label="Segment analysis">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <p className="kicker">Segments · all applications</p>
        <Select label="Group by" value={dim} className="w-60" options={SEGMENT_DIMS}
          onChange={(v) => setParams(v === DEFAULT_DIM ? {} : { dim: v }, { replace: true })} />
      </div>

      {/* Chart left, table right on xl (5 : 7 so the table needs no horizontal scroll); stacked below xl.
          Grid rows stretch, so both cards end at the same height; the table scrolls inside if it is longer. */}
      <div className="grid gap-6 xl:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
      <Card title="Default rate by segment"
        actions={<MousePointerClick className="h-4 w-4 text-ink-3" role="img" aria-label="Click a bar to explore this group" />}>
        {/* Previous dimension stays (dimmed) until the new one arrives: no skeleton swap, no height jump. */}
        {rows.data ? (
          <div className={clsx("transition-opacity duration-150", rows.loading && "opacity-50")} aria-busy={rows.loading}>
            <RateBars data={rows.data} avg={avg} layout={rows.data.length > 7 ? "bars" : "columns"} height={340} onSelect={openInExplorer} />
          </div>
        ) : rows.error ? <ErrorBox message={rows.error} /> : <ChartSkeleton height={340} />}
      </Card>

      <SegmentTable rows={rows.data} error={rows.error} dimLabel={dimLabel} loading={rows.loading} />
      </div>
    </section>
  );
}
