"""Quantity splitting for orders that exceed a broker's per-order limit.

Pure arithmetic, no broker knowledge — it takes the limits the resolver produced
and returns the minimum number of child chunks. The user never sees the chunks:
the execution engine submits them sequentially and reports one logical trade.
"""
from __future__ import annotations

import math

from services.broker_limits import OrderLimits


def split_quantity(qty: int, lots: int, limits: OrderLimits) -> list[tuple[int, int]]:
    """Return [(qty, lots), ...] — the child orders to submit, in order.

    A single-element list means "no split needed"; the caller then behaves
    exactly as it did before splitting existed.
    """
    qty = int(qty)
    lots = int(lots) or 0
    if qty <= 0:
        return [(qty, lots)]

    lot_size = int(qty / lots) if lots > 0 else 0
    cap = limits.cap_qty(lot_size)
    if not cap or cap <= 0 or qty <= cap:
        return [(qty, lots)]

    # Chunk on whole lots where we know the lot size, so no child order carries a
    # partial lot (exchanges reject those). Otherwise chunk on raw quantity.
    chunks: list[tuple[int, int]] = []
    if lot_size > 0:
        lots_per_chunk = max(1, cap // lot_size)
        remaining = lots
        while remaining > 0:
            take = min(lots_per_chunk, remaining)
            chunks.append((take * lot_size, take))
            remaining -= take
    else:
        remaining = qty
        while remaining > 0:
            take = min(cap, remaining)
            chunks.append((take, 0))
            remaining -= take
    return chunks


def chunk_count(qty: int, lots: int, limits: OrderLimits) -> int:
    lot_size = int(qty / lots) if lots else 0
    cap = limits.cap_qty(lot_size)
    return 1 if not cap else math.ceil(qty / cap)
