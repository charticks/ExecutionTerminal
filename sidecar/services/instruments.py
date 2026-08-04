"""Canonical instrument identity, independent of any one broker's token space.

Every broker names the same contract differently: Angel's instrument master
calls NIFTY 26AUG2026 24000 CE token "43492", Dhan's scrip master calls it
securityId "45123", Kotak calls it something else again. Until now the sidecar
used the *Angel token* as the de-facto primary key for market data —
``BrokerManager.option_ticks``, ``PaperEngine._pending_by_token`` and
``OptionChainAdapter._token_index`` were all keyed by it. That works exactly as
long as Angel is the only feed, and silently breaks the moment a second broker
streams the same contract under a different id.

``InstrumentKey`` is that primary key instead: derived from the contract's own
economics (underlying / expiry / strike / type), so two brokers quoting the
same option necessarily produce the same key. Each broker keeps a private
two-way map between its own tokens and these keys, registered here.

Nothing in this module talks to a broker or a socket — it is a pure lookup
table, so feeds, the option chain and the paper engine can all share one
vocabulary without depending on each other.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

INDEX = "INDEX"
OPT = "OPT"
FUT = "FUT"


@dataclass(frozen=True, slots=True)
class InstrumentKey:
    """Broker-agnostic identity for one tradable instrument.

    Frozen + slotted so it is hashable (these are dict keys on the hot tick
    path) and cheap to allocate. Field values are normalised by the
    constructors below — build keys through those, not by hand, or a
    lower-case underlying will quietly fail to match an upper-case one.
    """
    underlying: str          # "NIFTY", "SENSEX", "CRUDEOIL"
    segment: str             # INDEX | OPT | FUT
    expiry: str = ""         # broker-neutral "26AUG2026"; "" for INDEX
    strike: int = 0          # whole rupees; 0 for INDEX/FUT
    opt_type: str = ""       # "CE" | "PE"; "" for INDEX/FUT

    @classmethod
    def index(cls, underlying: str) -> "InstrumentKey":
        return cls(underlying=(underlying or "").upper(), segment=INDEX)

    @classmethod
    def option(cls, underlying: str, expiry: str, strike: float,
               opt_type: str) -> "InstrumentKey":
        return cls(
            underlying=(underlying or "").upper(),
            segment=OPT,
            expiry=(expiry or "").upper(),
            # int() not round(): strikes are whole rupees on every index
            # Charticks trades, and float noise from a /100 conversion must
            # not produce two distinct keys for one strike.
            strike=int(float(strike or 0)),
            opt_type=(opt_type or "").upper(),
        )

    @classmethod
    def future(cls, underlying: str, expiry: str) -> "InstrumentKey":
        return cls(underlying=(underlying or "").upper(), segment=FUT,
                   expiry=(expiry or "").upper())

    def __str__(self) -> str:
        if self.segment == INDEX:
            return self.underlying
        if self.segment == FUT:
            return f"{self.underlying} {self.expiry} FUT"
        return f"{self.underlying} {self.expiry} {self.strike} {self.opt_type}"


class InstrumentRegistry:
    """Two-way ``InstrumentKey`` <-> broker-token map, one namespace per broker.

    Thread-safe: feeds bind from their own connect/subscribe threads while the
    tick path reads concurrently.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._to_token: dict[str, dict[InstrumentKey, str]] = {}
        self._to_key: dict[str, dict[str, InstrumentKey]] = {}
        # Catalogue: which contracts exist at all, pooled across brokers.
        # underlying -> expiry -> {strike: {opt_type}}. This is what makes the
        # option chain broker-agnostic — it used to derive strikes and expiries
        # by scanning Angel's raw instrument-master rows, so the chain was empty
        # whenever Angel was not the connected broker. Rebuilt from bindings, so
        # any feed that binds its scrip master automatically populates it.
        self._catalogue: dict[str, dict[str, dict[int, set[str]]]] = {}

    def bind(self, broker: str, key: InstrumentKey, token: str) -> None:
        """Record that `broker` calls `key` by `token`.

        Rebinding a key drops its previous reverse entry, so a scrip-master
        refresh that reassigns a token can never leave the old token resolving
        to a live key (which would route another contract's ticks to it).
        """
        with self._lock:
            self._bind_locked(broker.lower(), key, str(token))

    def alias(self, broker: str, key: InstrumentKey, token: str) -> None:
        """Register an ADDITIONAL id that resolves to `key`, without changing
        which id we send on the wire.

        `bind` is one-to-one: rebinding a key drops the previous token's reverse
        entry, so calling it twice would silently discard the first id. Kotak
        needs both — it subscribes by trading symbol but its tick `tk` field has
        carried the numeric token in some segments — and a tick we cannot map is
        a contract with no price.
        """
        with self._lock:
            self._to_key.setdefault(broker.lower(), {})[str(token)] = key

    def alias_many(self, broker: str, pairs) -> None:
        broker = broker.lower()
        with self._lock:
            rev = self._to_key.setdefault(broker, {})
            for key, token in pairs:
                rev[str(token)] = key

    def bind_many(self, broker: str, pairs) -> None:
        """Bind an iterable of (key, token) under a single lock acquisition —
        the scrip-master path binds ~100k rows at once."""
        broker = broker.lower()
        with self._lock:
            for key, token in pairs:
                self._bind_locked(broker, key, str(token))

    def _bind_locked(self, broker: str, key: InstrumentKey, token: str) -> None:
        fwd = self._to_token.setdefault(broker, {})
        rev = self._to_key.setdefault(broker, {})
        stale = fwd.get(key)
        if stale is not None and stale != token:
            rev.pop(stale, None)
        fwd[key] = token
        rev[token] = key
        if key.segment == OPT:
            (self._catalogue
                 .setdefault(key.underlying, {})
                 .setdefault(key.expiry, {})
                 .setdefault(key.strike, set())
                 .add(key.opt_type))

    def _reindex_locked(self) -> None:
        """Rebuild the catalogue from whatever bindings remain.

        Done wholesale after a clear rather than incrementally: a contract may
        be listed by several brokers, so removing one broker's binding does not
        necessarily remove the contract, and unpicking that per key is both
        fiddly and easy to get subtly wrong.
        """
        cat: dict[str, dict[str, dict[int, set[str]]]] = {}
        for fwd in self._to_token.values():
            for key in fwd:
                if key.segment != OPT:
                    continue
                (cat.setdefault(key.underlying, {})
                    .setdefault(key.expiry, {})
                    .setdefault(key.strike, set())
                    .add(key.opt_type))
        self._catalogue = cat

    def token_for(self, broker: str, key: InstrumentKey) -> str | None:
        with self._lock:
            return self._to_token.get(broker.lower(), {}).get(key)

    def key_for(self, broker: str, token: str) -> InstrumentKey | None:
        with self._lock:
            return self._to_key.get(broker.lower(), {}).get(str(token))

    def key_for_any(self, token: str, prefer: str | None = None) -> InstrumentKey | None:
        """Resolve a broker token to a key without knowing which broker issued
        it. `prefer` is tried first so an id that happens to exist in two
        brokers' spaces resolves to the one actually serving data.

        Needed because callers like the paper engine hold a token they got from
        resolve_option and have no idea which broker's space it came from.
        """
        token = str(token)
        with self._lock:
            order = ([prefer.lower()] if prefer else []) + sorted(self._to_key)
            for broker in order:
                key = self._to_key.get(broker, {}).get(token)
                if key is not None:
                    return key
        return None

    def clear_broker(self, broker: str, segment: str | None = None) -> None:
        """Drop a broker's bindings — its whole token space, or just one
        segment. Used when a scrip master is reloaded, so expired contracts do
        not survive the refresh.

        Pass `segment` when only part of the space is being rebuilt: the option
        index is rebuilt daily from the scrip master, but index/spot tokens are
        bound once at login and must not be collateral damage.
        """
        broker = broker.lower()
        with self._lock:
            fwd = self._to_token.get(broker)
            if fwd is None:
                return
            if segment is None:
                self._to_token.pop(broker, None)
                self._to_key.pop(broker, None)
                self._reindex_locked()
                return
            rev = self._to_key.setdefault(broker, {})
            for key in [k for k in fwd if k.segment == segment]:
                rev.pop(fwd.pop(key), None)
            self._reindex_locked()

    # ── catalogue queries (broker-agnostic) ───────────────────────────────
    def underlyings(self) -> list[str]:
        """Every underlying with at least one tradable option bound."""
        with self._lock:
            return sorted(self._catalogue)

    def expiries(self, underlying: str) -> list[str]:
        """Still-tradable expiries for `underlying`, chronologically ascending.

        Pooled across every connected broker, so the chain populates from
        whichever one is connected. Expired contracts are filtered here rather
        than trusted from any broker's master — they keep listing yesterday's
        contracts well past the expiry session.
        """
        from services import expiry as expiry_filter
        with self._lock:
            found = list(self._catalogue.get((underlying or "").upper(), {}))
        return expiry_filter.active_expiries([e for e in found if e])

    def strikes(self, underlying: str, expiry: str) -> list[int]:
        """Ascending strikes listed for one underlying + expiry."""
        with self._lock:
            return sorted(self._catalogue
                          .get((underlying or "").upper(), {})
                          .get((expiry or "").upper(), {}))

    def has(self, key: InstrumentKey) -> bool:
        """True when SOME connected broker lists this contract."""
        with self._lock:
            if key.segment != OPT:
                return any(key in fwd for fwd in self._to_token.values())
            return key.opt_type in (self._catalogue
                                    .get(key.underlying, {})
                                    .get(key.expiry, {})
                                    .get(key.strike, set()))

    def brokers_for(self, key: InstrumentKey) -> list[str]:
        """Which brokers can quote/trade this contract."""
        with self._lock:
            return sorted(b for b, fwd in self._to_token.items() if key in fwd)

    def any_token(self, key: InstrumentKey) -> tuple[str, str] | None:
        """(broker, token) from any broker listing `key` — for callers that
        just need *a* resolvable identity rather than a specific broker's."""
        with self._lock:
            for broker, fwd in sorted(self._to_token.items()):
                tok = fwd.get(key)
                if tok is not None:
                    return broker, tok
        return None

    def stats(self) -> dict[str, int]:
        """Bound instrument count per broker, for /market-feed diagnostics."""
        with self._lock:
            return {b: len(m) for b, m in self._to_token.items()}


instruments = InstrumentRegistry()
