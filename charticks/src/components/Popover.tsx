import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type MutableRefObject,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";

/** Anchor-relative popover state: the trigger lives inside `wrapRef`. Clicks
 *  outside it (or Escape) close the popover.
 *
 *  `panelRef` matters only in floating mode (see Popover): the panel is then
 *  portaled to <body>, so it is no longer a DOM descendant of `wrapRef` and a
 *  click on a menu item would otherwise read as "outside" and close the menu
 *  before the item's onClick fired. Passing it to <Popover panelRef=…> keeps
 *  the panel counted as inside. Anchored popovers leave it null and behave
 *  exactly as before. */
export function usePopover<T extends HTMLElement = HTMLDivElement>() {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<T>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const target = e.target as Node;
      const inside =
        !!wrapRef.current?.contains(target) || !!panelRef.current?.contains(target);
      if (!inside) setOpen(false);
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

  return { open, setOpen, toggle: () => setOpen((o) => !o), wrapRef, panelRef };
}

/** Which viewport edge the panel is aligned to, and which way it opened. */
type Align = "left" | "right";
type Placement = "top" | "bottom";

interface Position {
  top: number;
  left: number;
  placement: Placement;
}

const GAP = 8; // breathing room between trigger and panel
const EDGE = 8; // minimum distance from the viewport edge

/** Place the panel against its anchor: below by default, flipped above only
 *  when it does not fit below AND there is genuinely more room above. Clamped
 *  horizontally so a wide panel near the right edge stays on screen. */
function computePosition(anchor: DOMRect, panel: DOMRect, align: Align): Position {
  const roomBelow = window.innerHeight - anchor.bottom - GAP - EDGE;
  const roomAbove = anchor.top - GAP - EDGE;
  const placement: Placement =
    panel.height <= roomBelow || roomBelow >= roomAbove ? "bottom" : "top";

  const top =
    placement === "bottom" ? anchor.bottom + GAP : anchor.top - GAP - panel.height;
  const rawLeft = align === "right" ? anchor.right - panel.width : anchor.left;
  const left = Math.min(
    Math.max(EDGE, rawLeft),
    Math.max(EDGE, window.innerWidth - panel.width - EDGE),
  );
  return { top: Math.max(EDGE, top), left, placement };
}

function FloatingPopover({
  anchorRef,
  panelRef,
  className,
  align,
  children,
}: {
  anchorRef: RefObject<HTMLElement>;
  panelRef?: MutableRefObject<HTMLDivElement | null>;
  className: string;
  align: Align;
  children: ReactNode;
}) {
  // Mutable (not RefObject): the ref callback below assigns it directly.
  const ownRef = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState<Position | null>(null);

  useLayoutEffect(() => {
    const place = () => {
      const anchor = anchorRef.current;
      const panel = ownRef.current;
      if (!anchor || !panel) return;
      setPos(
        computePosition(
          anchor.getBoundingClientRect(),
          panel.getBoundingClientRect(),
          align,
        ),
      );
    };
    place();
    window.addEventListener("resize", place);
    // Capture phase: the trigger may sit in a scrollable ancestor (a panel
    // body), whose scroll events never reach window in the bubble phase.
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [anchorRef, align]);

  return createPortal(
    <div
      ref={(node) => {
        ownRef.current = node;
        if (panelRef) panelRef.current = node;
      }}
      className={`popover popover-floating ${className}`}
      data-placement={pos?.placement ?? "bottom"}
      style={
        pos
          ? { top: pos.top, left: pos.left }
          : // First paint: laid out at full size but not painted, so it can be
            // measured without flashing in the wrong place.
            { top: 0, left: 0, visibility: "hidden" }
      }
    >
      {children}
    </div>,
    document.body,
  );
}

/**
 * Popover panel. Two modes:
 *
 * - **Anchored** (default) — absolutely positioned inside `.pop-wrap`. Fine
 *   when no ancestor clips it.
 * - **Floating** (`anchorRef` given) — portaled to <body> with fixed
 *   positioning measured against the anchor, flipping above/below based on
 *   free viewport space. Required whenever the trigger sits inside an
 *   `overflow: hidden/auto` container (`.panel`, `.pbody`), which would
 *   otherwise clip the panel and silently hide menu items.
 *
 * In floating mode also pass `panelRef` from usePopover so outside-click
 * detection still treats the portaled panel as "inside".
 */
export function Popover({
  open,
  className = "",
  children,
  anchorRef,
  panelRef,
  align = "left",
}: {
  open: boolean;
  className?: string;
  children: ReactNode;
  anchorRef?: RefObject<HTMLElement>;
  panelRef?: MutableRefObject<HTMLDivElement | null>;
  align?: Align;
}) {
  if (!open) return null;
  if (!anchorRef) return <div className={`popover ${className}`}>{children}</div>;
  return (
    <FloatingPopover anchorRef={anchorRef} panelRef={panelRef} className={className} align={align}>
      {children}
    </FloatingPopover>
  );
}
