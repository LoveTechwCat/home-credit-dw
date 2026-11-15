import clsx from "clsx";
import { ArrowDownUp } from "lucide-react";
import { useMemo, useState } from "react";
import { type DimRow } from "../lib/api";
import { label, num, pct } from "../lib/format";
import { MIN_RELIABLE } from "./charts";
import { Card, ErrorBox } from "./ui";

/** The 13 customer attributes worth comparing (no gender, weekday or grade dimensions). */
export const SEGMENT_DIMS: { value: string; label: string }[] = [
  { value: "age_bucket", label: "Age" },
  { value: "income_bracket", label: "Income bracket" },
  { value: "income_type", label: "Employment type" },
  { value: "occupation_type", label: "Occupation" },
  { value: "education_type", label: "Education" },
  { value: "family_status", label: "Family status" },
  { value: "housing_type", label: "Housing" },
  { value: "population_density_band", label: "Region density" },
  { value: "contract_type", label: "Contract type" },
  { value: "credit_history_status", label: "Credit history status" },
  { value: "max_dpd_bucket", label: "Worst days late" },
  { value: "bureau_active_bucket", label: "Active bureau credits" },
  { value: "prev_approval_bucket", label: "Previous approval rate" },
];

type SortKey = "member" | "n_applications" | "default_rate" | "total_credit";

export function SegmentTable({ rows: data, error, dimLabel, loading = false }: {
  rows?: DimRow[]; error?: string; dimLabel: string; loading?: boolean;
}) {
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "default_rate", dir: -1 });

  const rows = useMemo(() => {
    if (!data) return [];
    return [...data].sort((a, b) => {
      const x = a[sort.key] as number | string | null;
      const y = b[sort.key] as number | string | null;
      if (x == null) return 1;
      if (y == null) return -1;
      return (x < y ? -1 : x > y ? 1 : 0) * sort.dir;
    });
  }, [data, sort]);

  const max = Math.max(0, ...(data ?? []).map((r) => r.default_rate ?? 0));
  // Rows partition the filtered applications, so their sums are the shares' denominators.
  const totalApps = (data ?? []).reduce((s, r) => s + (r.n_applications ?? 0), 0);
  const totalCredit = (data ?? []).reduce((s, r) => s + (r.total_credit ?? 0), 0);

  const head = (key: SortKey, text: string, right = true) => (
    <th className={right ? "text-right" : "text-left"} aria-sort={sort.key === key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
      <button className="inline-flex items-center gap-1 uppercase tracking-[0.06em] hover:text-ink"
        onClick={() => setSort((s) => ({ key, dir: s.key === key ? (s.dir === 1 ? -1 : 1) : -1 }))}>
        {text} <ArrowDownUp className={sort.key === key ? "h-3 w-3 text-accent" : "h-3 w-3 opacity-40"} aria-hidden />
      </button>
    </th>
  );

  return (
    <Card title="Segment size">
      {error && <ErrorBox message={error} />}
      <div className={clsx("overflow-x-auto transition-opacity duration-150 xl:max-h-[400px] xl:overflow-y-auto", loading && "opacity-50")}
        aria-busy={loading}>
        <table className="table">
          <thead className="sticky top-0 z-10 bg-surface">
            <tr>
              {head("member", dimLabel, false)}
              {head("n_applications", "Applications")}
              <th className="text-right">Share</th>
              {head("default_rate", "Default rate")}
              {head("total_credit", "Share of credit")}
            </tr>
          </thead>
          <tbody className="num">
            {rows.map((r) => {
              const few = r.n_labelled < MIN_RELIABLE;
              return (
                <tr key={r.member}>
                  <td className="text-ink">
                    {label(r.member)}
                    {few && (
                      <span className="pill pill-neutral ml-2 px-2 text-[11px]" title={`Fewer than ${MIN_RELIABLE} historical loans: read with care`}>
                        few loans<span className="sr-only"> - fewer than {MIN_RELIABLE} historical loans, default rate is unreliable</span>
                      </span>
                    )}
                  </td>
                  <td className="text-right text-ink-2">{num(r.n_applications)}</td>
                  <td className="text-right text-ink-2">{totalApps ? pct(r.n_applications / totalApps) : "–"}</td>
                  <td className="text-right">
                    <div className="flex items-center justify-end gap-2">
                      <div className="hidden h-1.5 w-12 overflow-hidden rounded-full bg-raised sm:block 2xl:w-20" aria-hidden>
                        <div className={clsx("h-full rounded-full bg-accent", few && "opacity-40")}
                          style={{ width: `${max ? ((r.default_rate ?? 0) / max) * 100 : 0}%` }} />
                      </div>
                      <span className="w-14 text-ink">{pct(r.default_rate, 2)}</span>
                    </div>
                  </td>
                  <td className="text-right text-ink-2">{totalCredit && r.total_credit != null ? pct(r.total_credit / totalCredit) : "–"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
