import clsx from "clsx";
import { Eye, EyeOff, Plus, Search, X } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import type { CohortOp, CohortValue, ExploreField } from "../lib/api";
import { NO_VALUE_OPS, conditionText, opLabel, validateCondition, type UiCondition } from "../lib/cohort";
import { dateLabel, label, num } from "../lib/format";

const GROUP_ORDER = ["Customer", "Product", "Credit history", "Risk & decision", "Outcome"];

/**
 * Kibana-style cohort filter bar: "+ Add filter" → field picker → operator → value. Each condition is a chip
 * that can be edited (click), switched off (eye) or removed. Empty = all applications. Conditions listed in
 * `invalid` (e.g. from an edited link) are shown but never applied.
 */
export function CohortBar({ fields, conditions, invalid = new Set<number>(), onChange }: {
  fields: ExploreField[];
  conditions: UiCondition[];
  invalid?: Set<number>;
  onChange: (next: UiCondition[]) => void;
}) {
  const byKey = useMemo(() => new Map(fields.map((f) => [f.key, f])), [fields]);
  // null = closed; "new" = adding; number = editing that chip.
  const [open, setOpen] = useState<null | "new" | number>(null);
  const addRef = useRef<HTMLButtonElement>(null);
  const chipRefs = useRef<(HTMLButtonElement | null)[]>([]);
  /** Close; with `refocus` (Apply / Cancel / Escape) focus goes back to the control that opened it. */
  const close = (refocus: boolean) => {
    const from = open;
    setOpen(null);
    if (refocus) requestAnimationFrame(() => (from === "new" ? addRef.current : chipRefs.current[from as number] ?? addRef.current)?.focus());
  };

  const update = (i: number, c: UiCondition) => onChange(conditions.map((x, j) => (j === i ? c : x)));
  const remove = (i: number) => onChange(conditions.filter((_, j) => j !== i));

  return (
    <div className="card card-pad">
      <div className="flex flex-wrap items-center gap-2">
        <span className="mr-1 text-[13px] font-semibold text-ink">Cohort</span>
        {conditions.length === 0 && <span className="text-[13px] text-ink-2">All</span>}
        {conditions.map((c, i) => {
          const f = byKey.get(c.field);
          const text = conditionText(c, f);
          const bad = invalid.has(i);
          return (
            <span key={i} className="relative">
              <span className={clsx("inline-flex items-center gap-0.5 rounded-full border-[1.5px] bg-surface py-0.5 pl-1 pr-1 text-[12.5px]",
                bad ? "border-dashed border-bad text-ink-2" : c.enabled ? "border-accent text-ink" : "border-dashed border-line text-ink-2")}>
                <button type="button" className="rounded-full p-1 text-ink-2 hover:bg-raised hover:text-ink"
                  aria-pressed={c.enabled} aria-label={`${c.enabled ? "Disable" : "Enable"} filter ${text}`}
                  onClick={() => update(i, { ...c, enabled: !c.enabled })}>
                  {c.enabled ? <Eye className="h-3.5 w-3.5" aria-hidden /> : <EyeOff className="h-3.5 w-3.5" aria-hidden />}
                </button>
                <button type="button" ref={(el) => { chipRefs.current[i] = el; }}
                  className={clsx("max-w-[340px] truncate rounded px-1 font-semibold hover:underline", (!c.enabled || bad) && "line-through")}
                  onClick={() => setOpen(open === i ? null : i)} aria-label={`Edit filter ${text}${bad ? " (invalid, not applied)" : ""}`} title={text}>
                  {text}
                </button>
                {bad && <span className="pill pill-bad px-1.5 text-[10.5px]">invalid</span>}
                <button type="button" className="rounded-full p-1 text-ink-2 hover:bg-raised hover:text-ink"
                  aria-label={`Remove filter ${text}`} onClick={() => remove(i)}>
                  <X className="h-3.5 w-3.5" aria-hidden />
                </button>
              </span>
              {open === i && (
                <Popover label="Edit filter" onClose={close} ignore={() => [chipRefs.current[i]]}>
                  {f ? (
                    <ConditionEditor field={f} initial={c} onCancel={() => close(true)}
                      onApply={(next) => { update(i, { ...next, enabled: bad ? true : c.enabled }); close(true); }} />
                  ) : <p className="p-3 text-[13px] text-ink-2">This field is no longer available; remove the filter.</p>}
                </Popover>
              )}
            </span>
          );
        })}

        <span className="relative">
          <button ref={addRef} type="button" className="btn-add" aria-expanded={open === "new"} aria-haspopup="dialog"
            onClick={() => setOpen(open === "new" ? null : "new")}>
            <Plus className="h-4 w-4" aria-hidden /> Add filter
          </button>
          {open === "new" && (
            <Popover label="Add filter" onClose={close} ignore={() => [addRef.current]}>
              <AddFilter fields={fields} onCancel={() => close(true)}
                onApply={(c) => { onChange([...conditions, { ...c, enabled: true }]); close(true); }} />
            </Popover>
          )}
        </span>

        {conditions.length > 0 && (
          <button type="button" className="ml-auto text-[12.5px] font-semibold text-accent-ink hover:underline" onClick={() => onChange([])}>
            Clear all
          </button>
        )}
      </div>
    </div>
  );
}

/** Shift an absolutely positioned panel horizontally so it stays inside the viewport (phones, right edge). */
function useClampToViewport(ref: RefObject<HTMLElement>) {
  const [dx, setDx] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const margin = 8;
    const left = r.left - dx;
    const right = r.right - dx;
    if (right > window.innerWidth - margin) setDx(window.innerWidth - margin - right);
    else if (left < margin) setDx(margin - left);
    // Measured once per open; the panel does not move while open.
  }, []);
  return dx;
}

/**
 * Floating panel under its trigger, kept inside the viewport. Closes on outside click (focus stays where the
 * user clicked) or Escape (onClose(true): focus returns to the trigger).
 */
export function Popover({ children, onClose, label: name, align = "left", ignore }: {
  children: ReactNode;
  onClose: (refocus: boolean) => void;
  label: string;
  align?: "left" | "right";
  /** The trigger(s): a mousedown on them is not "outside", so their own click can toggle the popover closed. */
  ignore?: () => (Element | null | undefined)[];
}) {
  const ref = useRef<HTMLDivElement>(null);
  const dx = useClampToViewport(ref);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const ignoreRef = useRef(ignore);
  ignoreRef.current = ignore;
  useEffect(() => {
    const click = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!ref.current || ref.current.contains(t)) return;
      if (ignoreRef.current?.().some((el) => el?.contains(t))) return;
      closeRef.current(false);
    };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") closeRef.current(true); };
    // Defer so the click that opened the popover does not close it straight away.
    const t = setTimeout(() => document.addEventListener("mousedown", click));
    document.addEventListener("keydown", key);
    return () => { clearTimeout(t); document.removeEventListener("mousedown", click); document.removeEventListener("keydown", key); };
  }, []);
  return (
    <div ref={ref} role="dialog" aria-label={name} style={dx ? { transform: `translateX(${dx}px)` } : undefined}
      className={clsx("absolute top-full z-40 mt-2 w-[min(360px,calc(100vw-1rem))] rounded-[14px] border border-line bg-surface shadow-pop",
        align === "left" ? "left-0" : "right-0")}>
      {children}
    </div>
  );
}

/** Step 1: searchable, grouped field list; step 2: the condition editor for the chosen field. */
function AddFilter({ fields, onApply, onCancel }: {
  fields: ExploreField[];
  onApply: (c: { field: string; op: CohortOp; value: CohortValue }) => void;
  onCancel: () => void;
}) {
  const [picked, setPicked] = useState<ExploreField | null>(null);
  const [q, setQ] = useState("");
  if (picked) {
    return <ConditionEditor field={picked} onApply={onApply} onCancel={onCancel} onBack={() => setPicked(null)} />;
  }
  const match = fields.filter((f) => f.label.toLowerCase().includes(q.trim().toLowerCase()));
  const groups = [...new Set([...GROUP_ORDER, ...match.map((f) => f.group)])]
    .map((g) => ({ g, items: match.filter((f) => f.group === g) }))
    .filter((x) => x.items.length);
  return (
    <div className="p-3">
      <label className="relative block">
        <span className="sr-only">Search fields</span>
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-3" aria-hidden />
        <input autoFocus className="input pl-9" placeholder="Search fields" value={q} onChange={(e) => setQ(e.target.value)} />
      </label>
      <div className="mt-2 max-h-72 overflow-y-auto">
        {groups.length === 0 && <p className="p-2 text-[13px] text-ink-2">No field matches.</p>}
        {groups.map(({ g, items }) => (
          <div key={g} className="py-1">
            <p className="eyebrow px-2 py-1">{g}</p>
            {items.map((f) => (
              <button key={f.key} type="button" onClick={() => setPicked(f)}
                className="flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-left text-[13px] text-ink hover:bg-raised focus-visible:bg-raised focus-visible:outline-none">
                {f.label}
                <span className="text-[11px] text-ink-2">{f.type === "number" ? "number" : f.type === "boolean" ? "yes / no" : f.type === "date" ? "date" : "list"}</span>
              </button>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

function defaultValue(field: ExploreField, op: CohortOp): CohortValue {
  if (NO_VALUE_OPS.includes(op)) return null;
  if (field.type === "boolean") return true;
  if (op === "is_one_of" || op === "is_not_one_of") return [];
  if (field.type === "date") return op === "between" ? ["", ""] : "";
  if (op === "between") return [typeof field.min === "number" ? field.min : 0, typeof field.max === "number" ? field.max : 0];
  if (field.type === "number") return null;
  return "";
}

/** Operator select + a value input that matches the field type and operator. */
function ConditionEditor({ field, initial, onApply, onCancel, onBack }: {
  field: ExploreField;
  initial?: { op: CohortOp; value: CohortValue };
  onApply: (c: { field: string; op: CohortOp; value: CohortValue }) => void;
  onCancel: () => void;
  onBack?: () => void;
}) {
  const [op, setOp] = useState<CohortOp>(initial?.op ?? field.ops[0]);
  const [value, setValue] = useState<CohortValue>(initial ? initial.value : defaultValue(field, field.ops[0]));
  const [error, setError] = useState<string | null>(null);

  const changeOp = (next: CohortOp) => {
    setOp(next);
    // Keep compatible values (single → list), otherwise start from the operator's default.
    if ((next === "is_one_of" || next === "is_not_one_of") && typeof value === "string" && value) setValue([value]);
    else if ((next === "is" || next === "is_not") && Array.isArray(value) && typeof value[0] === "string") setValue(value[0] ?? "");
    else if (field.type === "number" && next !== "between" && typeof value === "number") setValue(value);
    else if (field.type === "date" && next !== "between" && typeof value === "string" && value) setValue(value);
    else setValue(defaultValue(field, next));
    setError(null);
  };

  const apply = () => {
    const c = { field: field.key, op, value: NO_VALUE_OPS.includes(op) ? null : value };
    const problem = validateCondition(c, field);
    if (problem) setError(problem);
    else onApply(c);
  };

  return (
    <form className="space-y-3 p-3" onSubmit={(e) => { e.preventDefault(); apply(); }} noValidate>
      <div className="flex items-center justify-between gap-2">
        <p className="text-[13.5px] font-semibold text-ink">{field.label}</p>
        {onBack && <button type="button" className="text-[12px] font-semibold text-accent-ink hover:underline" onClick={onBack}>Change field</button>}
      </div>
      <label className="flex flex-col gap-1">
        <span className="text-xs font-semibold text-ink-2">Operator</span>
        <select autoFocus className="input cursor-pointer" value={op} onChange={(e) => changeOp(e.target.value as CohortOp)}>
          {field.ops.map((o) => <option key={o} value={o}>{opLabel(o, field)}</option>)}
        </select>
      </label>

      {!NO_VALUE_OPS.includes(op) && <ValueInput field={field} op={op} value={value} onChange={(v) => { setValue(v); setError(null); }} />}

      {error && <p className="text-[12.5px] text-bad" role="alert">{error}</p>}
      <div className="flex justify-end gap-2 pt-1">
        <button type="button" className="btn text-xs" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn-primary text-xs">Apply</button>
      </div>
    </form>
  );
}

function ValueInput({ field, op, value, onChange }: {
  field: ExploreField; op: CohortOp; value: CohortValue; onChange: (v: CohortValue) => void;
}) {
  if (field.type === "boolean") {
    return (
      <fieldset className="flex gap-2">
        <legend className="sr-only">Value</legend>
        {[true, false].map((b) => (
          <button key={String(b)} type="button" className="chip" aria-pressed={value === b} onClick={() => onChange(b)}>
            {b ? "Yes" : "No"}
          </button>
        ))}
      </fieldset>
    );
  }

  if (field.type === "date") {
    // Native date inputs; values travel as ISO "YYYY-MM-DD".
    const minD = typeof field.min === "string" ? field.min.slice(0, 10) : undefined;
    const maxD = typeof field.max === "string" ? field.max.slice(0, 10) : undefined;
    const hint = minD && maxD ? `${dateLabel(minD)} – ${dateLabel(maxD)}` : null;
    if (op === "between") {
      const pair = (Array.isArray(value) ? value : ["", ""]) as [string, string];
      return (
        <div className="space-y-1">
          <div className="grid grid-cols-2 gap-2">
            <label className="flex flex-col gap-1"><span className="text-xs font-semibold text-ink-2">From</span>
              <input type="date" className="input num" min={minD} max={maxD} value={typeof pair[0] === "string" ? pair[0] : ""}
                onChange={(e) => onChange([e.target.value, typeof pair[1] === "string" ? pair[1] : ""])} /></label>
            <label className="flex flex-col gap-1"><span className="text-xs font-semibold text-ink-2">To</span>
              <input type="date" className="input num" min={minD} max={maxD} value={typeof pair[1] === "string" ? pair[1] : ""}
                onChange={(e) => onChange([typeof pair[0] === "string" ? pair[0] : "", e.target.value])} /></label>
          </div>
          {hint && <p className="text-[12px] text-ink-2">{hint}</p>}
        </div>
      );
    }
    return (
      <label className="flex flex-col gap-1">
        <span className="text-xs font-semibold text-ink-2">Date</span>
        <input type="date" className="input num" min={minD} max={maxD} value={typeof value === "string" ? value : ""}
          onChange={(e) => onChange(e.target.value)} />
        {hint && <span className="text-[12px] text-ink-2">{hint}</span>}
      </label>
    );
  }

  if (field.type === "number") {
    const hint = typeof field.min === "number" && typeof field.max === "number" ? `Range ${num(field.min, 2)} – ${num(field.max, 2)}` : null;
    const parse = (s: string) => (s.trim() === "" ? NaN : Number(s));
    if (op === "between") {
      const pair = (Array.isArray(value) ? value : [NaN, NaN]) as [number, number];
      const show = (n: number) => (Number.isFinite(n) ? String(n) : "");
      return (
        <div className="space-y-1">
          <div className="grid grid-cols-2 gap-2">
            <label className="flex flex-col gap-1"><span className="text-xs font-semibold text-ink-2">From</span>
              <input className="input num" inputMode="decimal" defaultValue={show(pair[0])}
                onChange={(e) => onChange([parse(e.target.value), pair[1]])} /></label>
            <label className="flex flex-col gap-1"><span className="text-xs font-semibold text-ink-2">To</span>
              <input className="input num" inputMode="decimal" defaultValue={show(pair[1])}
                onChange={(e) => onChange([pair[0], parse(e.target.value)])} /></label>
          </div>
          {hint && <p className="text-[12px] text-ink-2">{hint}</p>}
        </div>
      );
    }
    return (
      <label className="flex flex-col gap-1">
        <span className="text-xs font-semibold text-ink-2">Value</span>
        <input className="input num" inputMode="decimal" defaultValue={typeof value === "number" ? String(value) : ""}
          onChange={(e) => onChange(parse(e.target.value))} />
        {hint && <span className="text-[12px] text-ink-2">{hint}</span>}
      </label>
    );
  }

  // Category
  const multi = op === "is_one_of" || op === "is_not_one_of";
  if (!field.values?.length) {
    return (
      <label className="flex flex-col gap-1">
        <span className="text-xs font-semibold text-ink-2">{multi ? "Values (comma-separated)" : "Value"}</span>
        <input className="input" defaultValue={Array.isArray(value) ? value.join(", ") : typeof value === "string" ? value : ""}
          onChange={(e) => onChange(multi ? e.target.value.split(",").map((x) => x.trim()).filter(Boolean) : e.target.value)} />
      </label>
    );
  }
  return <ValueList values={field.values} multi={multi} value={value} onChange={onChange} />;
}

/** Searchable list of category values: radio-like for one value, checkboxes for several. */
function ValueList({ values, multi, value, onChange }: {
  values: string[]; multi: boolean; value: CohortValue; onChange: (v: CohortValue) => void;
}) {
  const [q, setQ] = useState("");
  const chosen = new Set(Array.isArray(value) ? (value as string[]) : typeof value === "string" && value ? [value] : []);
  const shown = values.filter((v) => label(v).toLowerCase().includes(q.trim().toLowerCase()));
  const toggle = (v: string) => {
    if (!multi) return onChange(v);
    const next = new Set(chosen);
    if (next.has(v)) next.delete(v); else next.add(v);
    onChange(values.filter((x) => next.has(x)));
  };
  return (
    <div>
      {values.length > 8 && (
        <label className="relative mb-2 block">
          <span className="sr-only">Search values</span>
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-3" aria-hidden />
          <input className="input pl-9" placeholder="Search values" value={q} onChange={(e) => setQ(e.target.value)} />
        </label>
      )}
      <div className="max-h-56 overflow-y-auto rounded-lg border border-line p-1" role={multi ? "group" : "radiogroup"} aria-label="Values">
        {shown.length === 0 && <p className="p-2 text-[13px] text-ink-2">No value matches.</p>}
        {shown.map((v) => (
          <label key={v} className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-[13px] text-ink hover:bg-raised">
            <input type={multi ? "checkbox" : "radio"} name="cohort-value" className="h-4 w-4 accent-[rgb(var(--accent))]"
              checked={chosen.has(v)} onChange={() => toggle(v)} />
            {label(v)}
          </label>
        ))}
      </div>
      {multi && <p className="mt-1 text-[12px] text-ink-2">{chosen.size} selected</p>}
    </div>
  );
}
