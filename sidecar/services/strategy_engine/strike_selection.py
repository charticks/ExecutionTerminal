"""Strike selection — ported from `legacy/app/option_chain.py:203-266`
(`calculate_atm`/`is_institutional_round`/`generate_strikes`), unchanged.

Pure math: given a spot price and a preset's strike-selection params, produce
a list of `(strike, opt_type)` tuples. `map_strikes_to_tokens` (the function
right after these in legacy) is deliberately NOT ported — it was an
Angel-specific instrument-master scan. Resolving a `(strike, opt_type)` to a
tradable contract is `services.instruments`' job, broker-agnostically,
already built and proven across five brokers; nothing here needs to know
what a broker's master looks like.
"""
from __future__ import annotations

# index -> strike step. Matches option_chain.py's own table exactly.
_STEP = {"SENSEX": 100, "NIFTY": 50, "BANKNIFTY": 100, "CRUDEOIL": 50}
# index -> the modulus an ATM must divide evenly by to count as an
# "institutional round number" for ROUND mode.
_ROUND_MOD = {"SENSEX": 500, "NIFTY": 100, "BANKNIFTY": 500}
# LEGACY mode's per-index, per-level point offsets from ATM.
_LEGACY_GAP = {
    "NIFTY": {"ATM": 0, "+1": 50, "-1": -50, "+2": 100, "-2": -100},
    "BANKNIFTY": {"ATM": 0, "+1": 100, "-1": -100, "+2": 200, "-2": -200},
    "SENSEX": {"ATM": 0, "+1": 100, "-1": -100, "+2": 200, "-2": -200},
}


def calculate_atm(spot: float, index: str) -> tuple[int, int]:
    """(atm_strike, step) — spot rounded to the index's strike step."""
    step = _STEP.get(index.upper(), 50)
    atm = round(spot / step) * step
    return int(atm), step


def is_institutional_round(atm: int, index: str) -> bool:
    mod = _ROUND_MOD.get(index.upper(), 1)
    return atm % mod == 0


def generate_strikes(atm: int, step: int, index: str, params: dict) -> list[tuple[int, str]]:
    """One of four modes, selected by `params["strike_mode"]` — ROUND,
    LEGACY, RELATIVE, CUSTOM_RANGE. Same four, same math, as legacy."""
    mode = params.get("strike_mode", "ROUND")
    index = index.upper()
    strikes: list[tuple[int, str]] = []

    if mode == "ROUND":
        if is_institutional_round(atm, index):
            strikes += [(atm, "CE"), (atm, "PE")]
        return strikes

    if mode == "LEGACY":
        gaps = _LEGACY_GAP.get(index, {"ATM": 0})
        legacy_gap_vars = params.get("legacy_gap_vars") or {}
        for level, gap in gaps.items():
            if not legacy_gap_vars.get(level):
                continue
            sp = atm + gap
            if sp % step == 0:
                strikes += [(sp, "CE"), (sp, "PE")]
        return strikes

    if mode == "RELATIVE":
        directional_vars = params.get("directional_vars") or {}
        for level, types in directional_vars.items():
            for opt_type, enabled in (types or {}).items():
                if not enabled:
                    continue
                if level == "ATM":
                    sp = atm
                else:
                    direction = 1 if level.startswith("+") else -1
                    gap = int(level.replace("+", "").replace("-", ""))
                    sp = atm + direction * gap
                if sp % step == 0:
                    strikes.append((sp, opt_type))
        return strikes

    if mode == "CUSTOM_RANGE":
        from_offset = int(params.get("custom_range_from", 0))
        to_offset = int(params.get("custom_range_to", 0))
        include_ce = bool(params.get("custom_range_ce"))
        include_pe = bool(params.get("custom_range_pe"))
        lo, hi = min(from_offset, to_offset), max(from_offset, to_offset)
        offset = lo
        while offset <= hi:
            sp = atm + offset
            if sp % step == 0:
                if include_ce:
                    strikes.append((sp, "CE"))
                if include_pe:
                    strikes.append((sp, "PE"))
            offset += step
        return strikes

    return strikes
