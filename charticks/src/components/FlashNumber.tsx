import { useEffect, useRef } from "react";

/** A monospace number that pulses green/red when its value changes. */
export function FlashNumber({
  value,
  format,
  className = "",
}: {
  value: number;
  format: (n: number) => string;
  className?: string;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const prev = useRef(value);

  useEffect(() => {
    const el = ref.current;
    if (!el || value === prev.current) return;
    const up = value > prev.current;
    prev.current = value;
    el.classList.remove("flash-up", "flash-down");
    void el.offsetWidth; // restart animation
    el.classList.add(up ? "flash-up" : "flash-down");
  }, [value]);

  return (
    <span ref={ref} className={`num ${className}`}>
      {format(value)}
    </span>
  );
}
