import clsx from "clsx";
import { ArrowRight, Check, ChevronDown, Copy, Search } from "lucide-react";
import { useRef, useState, type FormEvent, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import {
  ApplicantFacts, DecisionBadge, DecisionBand, DssError, GradeSection,
} from "../components/AssessmentView";
import { GradeChip } from "../components/charts";
import { Popover } from "../components/CohortBar";
import { useApi, type Assessment as AssessmentT, type Decision, type Grade } from "../lib/api";
import { dateLabel } from "../lib/format";

/** Real cases with the trained model (checked against the live API), labelled by their outcome. */
const EXAMPLES: { id: string; from: Grade; to?: Grade; decision: Decision; note?: string }[] = [
  { id: "428281", from: "B", to: "D", decision: "review" },
  { id: "257416", from: "C", to: "D", decision: "review" },
  { id: "100690", from: "E", decision: "reject" },
  { id: "100001", from: "B", decision: "approve", note: "no outcome yet" },
  { id: "100009", from: "B", decision: "approve", note: "amount cut" },
];
const DECISION_WORD: Record<Decision, string> = { approve: "Approve", review: "Manual review", reject: "Reject" };
const exampleOutcome = (x: (typeof EXAMPLES)[number]) =>
  [x.to ? `${x.from} → ${x.to}` : x.from, DECISION_WORD[x.decision], x.note].filter(Boolean).join(" · ");

/** SK_ID_CURR is a positive integer; anything else never reaches the API. */
const isValidId = (v: string) => /^\d{1,9}$/.test(v) && Number(v) > 0;

/**
 * Application details (/applications-details?id=…): one application, top to bottom: decision band, applicant,
 * how the grade was set, risk drivers. Calls only GET /api/assessment/{id}.
 */
export default function ApplicationDetails() {
  const [params, setParams] = useSearchParams();
  const rawId = params.get("id")?.trim() ?? null;
  const id = rawId && isValidId(rawId) ? rawId : null;
  const badUrlId = rawId != null && rawId !== "" && !id;
  const [inputError, setInputError] = useState<string | null>(null);
  const res = useApi<AssessmentT>(id ? `/api/assessment/${encodeURIComponent(id)}` : null);

  const open = (value: string) => {
    const v = value.trim();
    if (!isValidId(v)) {
      setInputError("Enter a positive whole number, e.g. 428281.");
      return false;
    }
    setInputError(null);
    setParams({ id: v });
    return true;
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        {id ? (
          <div>
            <p className="text-[12.5px] font-medium text-ink-2">Application</p>
            <div className="flex items-center gap-1.5">
              <h1 className="num text-[24px] font-extrabold leading-[1.1] tracking-[-0.02em] text-ink sm:text-[28px]">
                <span className="sr-only">Application </span>#{id}
              </h1>
              <CopyId id={id} />
            </div>
            {res.data?.profile?.application_date && (
              <p className="num mt-0.5 text-[12.5px] text-ink-2">Applied {dateLabel(res.data.profile.application_date)}</p>
            )}
          </div>
        ) : (
          <h1 className="text-[24px] font-extrabold leading-[1.1] tracking-[-0.02em] text-ink sm:text-[28px]">Application details</h1>
        )}
        <CompactSearch key={id ?? ""} initial={rawId ?? ""} onOpen={open} error={inputError ?? (badUrlId ? `Invalid application id “${rawId}”.` : null)} />
      </div>

      {!id && <EmptyState onOpen={open} current={id} />}

      {id && res.loading && !res.data && <DetailsSkeleton />}
      {id && res.error && (
        <DssError status={res.status} message={res.error}
          notFound={<>No application with id <b className="num">{id}</b>.</>} />
      )}
      {id && res.data && <Details a={res.data} />}
    </div>
  );
}

/** Small button that copies the application id; the label confirms for two seconds. */
function CopyId({ id }: { id: string }) {
  const [done, setDone] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(id).then(() => { setDone(true); setTimeout(() => setDone(false), 2000); }, () => undefined);
  };
  return (
    <button type="button" onClick={copy} className="rounded-md p-1.5 text-ink-2 hover:bg-raised hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
      aria-label={done ? "Application id copied" : "Copy application id"} title={done ? "Copied" : "Copy id"}>
      {done ? <Check className="h-4 w-4 text-good" aria-hidden /> : <Copy className="h-4 w-4" aria-hidden />}
      <span className="sr-only" aria-live="polite">{done ? "Copied" : ""}</span>
    </button>
  );
}

/** Id input + "Examples" menu, right of the title. */
function CompactSearch({ initial, onOpen, error }: { initial: string; onOpen: (v: string) => boolean; error: string | null }) {
  const [draft, setDraft] = useState(initial);
  const [menu, setMenu] = useState(false);
  const btn = useRef<HTMLButtonElement>(null);
  const submit = (e: FormEvent) => { e.preventDefault(); onOpen(draft); };
  const close = (refocus: boolean) => { setMenu(false); if (refocus) requestAnimationFrame(() => btn.current?.focus()); };
  return (
    <div className="w-full sm:w-auto">
      <form onSubmit={submit} noValidate className="flex flex-wrap items-center gap-2">
        <label className="relative">
          <span className="sr-only">Application id</span>
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-3" aria-hidden />
          <input className="input num w-44 pl-9" inputMode="numeric" value={draft} placeholder="Application id"
            aria-invalid={!!error} aria-describedby={error ? "id-error" : undefined} onChange={(e) => setDraft(e.target.value)} />
        </label>
        <button type="submit" className="btn btn-primary">Open</button>
        <span className="relative">
          <button ref={btn} type="button" className="btn" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((m) => !m)}>
            Examples <ChevronDown className="h-3.5 w-3.5" aria-hidden />
          </button>
          {menu && (
            <Popover label="Examples" align="right" onClose={close} ignore={() => [btn.current]}>
              <ul role="menu" className="p-1.5">
                {EXAMPLES.map((x) => (
                  <li key={x.id} role="none">
                    <button type="button" role="menuitem" autoFocus={x === EXAMPLES[0]}
                      className="flex w-full items-center justify-between gap-3 rounded-lg px-2.5 py-2 text-left text-[13px] hover:bg-raised focus-visible:bg-raised focus-visible:outline-none"
                      onClick={() => { setDraft(x.id); if (onOpen(x.id)) close(true); }}>
                      <span className="num font-semibold text-ink">{x.id}</span>
                      <span className="truncate text-ink-2">{exampleOutcome(x)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </Popover>
          )}
        </span>
      </form>
      {error && <p id="id-error" className="mt-1.5 text-[12.5px] text-bad">{error}</p>}
    </div>
  );
}

/** No application open: the example cards, shown only here. */
function EmptyState({ onOpen, current }: { onOpen: (v: string) => boolean; current: string | null }) {
  return (
    <section className="card card-pad" aria-labelledby="examples-title">
      <h2 id="examples-title" className="text-[15px] font-bold text-ink">Examples</h2>
      <ul className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
        {EXAMPLES.map((x) => (
          <li key={x.id}>
            <button type="button" onClick={() => onOpen(x.id)} aria-pressed={current === x.id}
              className="flex h-full w-full flex-col items-start gap-1.5 rounded-xl border-[1.5px] border-line bg-surface p-3 text-left transition hover:-translate-y-0.5 hover:border-accent hover:shadow-lift focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50">
              <span className="num text-[14px] font-bold text-ink">{x.id}</span>
              <span className="flex items-center gap-1 text-[12px] text-ink-2">
                <GradeChip grade={x.from} size="sm" />
                {x.to && <><ArrowRight className="h-3 w-3" aria-hidden /><span className="sr-only">downgraded to</span><GradeChip grade={x.to} size="sm" /></>}
              </span>
              <DecisionBadge decision={x.decision} size="sm" />
              {x.note && <span className="text-[11.5px] text-ink-2">{x.note}</span>}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** A card with a single sentence-case heading (no uppercase kickers on this page). */
function Panel({ title, children, className, compact }: { title?: string; children: ReactNode; className?: string; compact?: boolean }) {
  return (
    <section className={clsx("card", compact ? "px-5 py-4" : "card-pad", className)} aria-label={title}>
      {title && <h2 className={clsx("text-[15px] font-bold text-ink", compact ? "mb-2.5" : "mb-4")}>{title}</h2>}
      {children}
    </section>
  );
}

function Details({ a }: { a: AssessmentT }) {
  return (
    <div className="space-y-4">
      <Panel compact><DecisionBand a={a} /></Panel>
      <Panel title="Applicant" compact><ApplicantFacts p={a.profile} creditToIncome={a.credit_to_income} /></Panel>
      <GradeSection a={a} />
    </div>
  );
}

/** Loading placeholder with the same blocks as the loaded page. */
function DetailsSkeleton() {
  const block = (h: string) => <div className={clsx("animate-pulse rounded-[14px] bg-raised", h)} />;
  return (
    <div className="space-y-4" role="status" aria-label="Loading application" aria-busy="true">
      {block("h-28")}
      {block("h-40")}
      {block("h-80")}
    </div>
  );
}
