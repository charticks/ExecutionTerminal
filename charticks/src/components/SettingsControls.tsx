import { useState, type ReactNode } from "react";

// Compact, reusable form primitives for the Settings page. No external UI lib
// exists in the app, so these are intentionally small and style-token driven.

/** Labelled field wrapper — stacks a small caption above any control. */
export function Field({
  label,
  hint,
  children,
  className = "",
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={`sf-field ${className}`}>
      <span className="sf-label">
        {label}
        {hint && <span className="sf-hint">{hint}</span>}
      </span>
      {children}
    </label>
  );
}

/** iOS-style on/off switch. */
export function Toggle({
  checked,
  onChange,
  label,
  "aria-label": ariaLabel,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: string;
  "aria-label"?: string;
}) {
  const sw = (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel ?? label}
      className={`sf-switch ${checked ? "on" : ""}`}
      onClick={() => onChange(!checked)}
    >
      <span className="sf-knob" />
    </button>
  );
  if (!label) return sw;
  return (
    <span className="sf-toggle-row">
      <span className="sf-toggle-label">{label}</span>
      {sw}
    </span>
  );
}

/** Segmented control — a horizontal group of mutually exclusive options.
 *  `compact` shrinks the padding for dense cards; `disabled` greys it out
 *  (the control stays visible so the setting is still discoverable). */
export function Segmented<T extends string>({
  value,
  options,
  onChange,
  compact = false,
  disabled = false,
  "aria-label": ariaLabel,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  compact?: boolean;
  disabled?: boolean;
  "aria-label"?: string;
}) {
  return (
    <div
      className={`sf-segmented ${compact ? "compact" : ""} ${disabled ? "off" : ""}`}
      role="radiogroup"
      aria-label={ariaLabel}
    >
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          disabled={disabled}
          className={value === o.value ? "on" : ""}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** Checkbox + label, used to switch a whole feature (Stop Loss, Target, Trail
 *  SL, Portfolio Trail Profit) on or off. The box always precedes the label. */
export function Check({
  checked,
  onChange,
  label,
  className = "",
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  className?: string;
}) {
  return (
    <label className={`sf-check ${className}`}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}

/** One switchable feature on a single row: checkbox + label on the left, its
 *  controls right-aligned. Unchecked greys the controls out rather than hiding
 *  them, so the card's shape never changes as features are toggled.
 *  `lockReason` marks a feature that cannot be enabled yet (its checkbox stays
 *  clickable so the click can explain why). */
export function FeatureBlock({
  label,
  checked,
  onChange,
  lockReason,
  children,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  lockReason?: string;
  children: ReactNode;
}) {
  return (
    <div className={`sf-feature ${checked ? "" : "off"} ${lockReason ? "locked" : ""}`}>
      <span className="sf-feature-head">
        <Check checked={checked} onChange={onChange} label={label} />
        {lockReason && <span className="sf-lock" title={lockReason}>{lockReason}</span>}
      </span>
      <div className="sf-feature-body">{children}</div>
    </div>
  );
}

/** Disclosure row for rarely-touched settings. Collapsed on first render. */
export function Collapsible({ title, children }: { title: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="sf-collapse">
      <button
        type="button"
        className="sf-collapse-head"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className={`sf-caret ${open ? "open" : ""}`}>▶</span>
        {title}
      </button>
      {open && <div className="sf-collapse-body">{children}</div>}
    </div>
  );
}

/** Section divider between groups of cards on the Settings page. */
export function SectionTitle({ children }: { children: ReactNode }) {
  return <h4 className="sf-section">{children}</h4>;
}

/** Numeric input that keeps a string draft and commits non-negative integers. */
export function NumInput({
  value,
  onChange,
  min = 0,
  allowDecimal = false,
  disabled = false,
  "aria-label": ariaLabel,
}: {
  value: number;
  onChange: (n: number) => void;
  min?: number;
  allowDecimal?: boolean;
  disabled?: boolean;
  "aria-label"?: string;
}) {
  return (
    <input
      className="sf-num num"
      inputMode={allowDecimal ? "decimal" : "numeric"}
      value={String(value)}
      disabled={disabled}
      aria-label={ariaLabel}
      onChange={(e) => {
        const raw = e.target.value.replace(allowDecimal ? /[^\d.]/g : /\D/g, "");
        const n = allowDecimal ? parseFloat(raw) : parseInt(raw, 10);
        onChange(Number.isNaN(n) ? 0 : Math.max(min, n));
      }}
    />
  );
}

/** A titled settings card. `span` is its width in columns of the page's
 *  12-column card grid (it collapses to full width on narrow windows). */
export function Card({
  title,
  hint,
  children,
  span = 12,
}: {
  title: string;
  hint?: string;
  children: ReactNode;
  span?: number;
}) {
  return (
    <section className="sf-card" style={{ gridColumn: `span ${span}` }}>
      <div className="sf-card-head">
        <h4>{title}</h4>
        {hint && <span className="sf-card-hint">{hint}</span>}
      </div>
      {/* Cards in a row stretch to the tallest one; this body is the flex
          container that lets each card's content distribute over that height. */}
      <div className="sf-card-body">{children}</div>
    </section>
  );
}
