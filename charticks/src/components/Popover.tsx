import { useEffect, useRef, useState, type ReactNode, type RefObject } from "react";

/** Anchor-relative popover state: trigger + panel live inside `wrapRef`;
 *  clicks outside it (or Escape) close the popover. */
export function usePopover<T extends HTMLElement = HTMLDivElement>() {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<T>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return { open, setOpen, toggle: () => setOpen((o) => !o), wrapRef };
}

export function Popover({
  open,
  className = "",
  children,
}: {
  open: boolean;
  className?: string;
  children: ReactNode;
}) {
  if (!open) return null;
  return <div className={`popover ${className}`}>{children}</div>;
}

export type PopoverWrapRef = RefObject<HTMLDivElement>;
