"""optuna_tune/prompt_transfer.py - the prompt path's bus traffic, read from the DONE line.

Ported from HyperCore-Tune-Maya transfer.py (balance_score, pareto_front, frontier, knee).
The Maya version parses "STAT tok_s=" telemetry, which the HyperCore engine never prints
(0 matches across the 1168 run logs in optuna_tune/results/), so every number here comes
from the DONE line instead:

    DONE <generated> <prompt> <prompt_ms> <decode_ms> <finish> <drafts accepted>
         <drafts offered> <reused> <cache hits> <cache lookups> <RAM blobs> <file blobs>
         <file MB> <prompt tokens read> <offloaded>
         src/program/generate.cpp:10353-10370

At 20480 prompt tokens the prompt path moves ~25 GB of pack bytes per request, so prefill
is limited by the read rate rather than the compute. The machine's own read rate is
measured by the engine at boot:

    pre-filled 13061 of 13061 slots from the profile in 11.3 s (1598 MB/s)

which gives a per-byte cost with no external calibration. io_ms is the part of the prompt
time that the file traffic accounts for at that rate; balance_score discounts the measured
prefill rate by that share, so a configuration that is fast because it thrashes the bus
scores below one that is fast because it does not.

Nothing here is required: with no rate line, or no file bytes, the transfer is inert and
balance_score returns the plain rate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# How much of the discount --balance applies to the transfer share of a prompt.
TRANSFER_WEIGHT: float = 0.35

# DONE positions that carry transfer counters. Positions 1-8 and 14 are already read by
# engine_runner.DONE_FIELDS; these are the ones the balance objective adds.
DONE_TRANSFER_FIELDS: dict[int, str] = {
    9: "cache_hits", 10: "cache_lookups", 11: "ram_blobs", 12: "file_blobs",
    13: "file_mb", 15: "offloaded",
}

# The engine's own measurement of this machine's pack read rate, taken at boot.
PROFILE_FILL_RE = re.compile(
    r"pre-filled \d+ of \d+ slots from the profile in ([\d.]+) s \(([\d.]+) MB/s\)"
)
DRAFT_READ_RE = re.compile(r"files read in ([\d.]+) s \(([\d.]+) MiB/s\)")

# Columns a trial record gains from the telemetry, in the order as_dict() writes them.
# off_card_pct is the share of the decode's routed experts the GPU did NOT get from its own
# VRAM. Two counters carry it, and which one is live depends on the decode path:
#   * [offloaded] (DONE position 15, #588) is incremented only by the CPU-side routing loop
#     (src/core/expert_source.cpp:2801). On a box running the token-graph hit path - "the GPU
#     computes the experts in the cache", as this one does - that loop never runs, so the
#     field is 0 for every trial even at --pcie-frac 0.26.
#   * [lookups] - [hits] is the experts the cache did not already hold: admitted (filled from
#     the pack over PCIe, expert_source.cpp:2912) or refused (computed by the CPU pool).
#     That is the off-card traffic on the hit path, and it is the number the engine prints as
#     its decode hit rate (src/program/generate.cpp:10435-10439).
# off_card_pct is the union of the two, so a run measured on either path reports a share.
TRANSFER_COLUMNS: tuple[str, ...] = (
    "machine_mb_per_s", "file_mb", "file_mb_per_tok", "io_ms", "io_share",
    "cache_hit_rate", "cache_lookups", "cache_hits", "ram_blobs", "file_blobs",
    "offloaded", "off_card_pct",
)


def parse_machine_rate(lines: Iterable[str]) -> float | None:
    """MB/s this machine reads the pack at, as the engine measured it during boot.

    The profile fill is the largest and most representative read, so it wins over the
    draft-head line when both are present. Returns None when neither line appears.
    """
    profile: list[float] = []
    draft: list[float] = []
    for line in lines:
        m = PROFILE_FILL_RE.search(line)
        if m:
            try:
                rate = float(m.group(2))
            except ValueError:
                continue
            if rate > 0:
                profile.append(rate)
            continue
        m = DRAFT_READ_RE.search(line)
        if m:
            try:
                rate = float(m.group(2))
            except ValueError:
                continue
            if rate > 0:
                draft.append(rate)
    if profile:
        return max(profile)
    return max(draft) if draft else None


def stable_machine_rate(override: float | None, prior_rates: Iterable[float]) -> float | None:
    """One pack read-rate anchor for a whole sweep.

    A single boot line is not a machine constant: across the 75 October runs on this box
    the engine reported 1372-2926 MB/s, a 2.1x spread, so io_ms built from one boot line
    makes two identical configs incomparable. The median of everything this machine has
    recorded is used instead; an explicit override wins, and with no history at all the
    caller falls back to the run's own boot line.
    """
    if override is not None and override > 0:
        return float(override)
    rates = sorted(float(r) for r in prior_rates if isinstance(r, (int, float)) and r > 0)
    if not rates:
        return None
    mid = len(rates) // 2
    return rates[mid] if len(rates) % 2 else (rates[mid - 1] + rates[mid]) / 2.0


@dataclass
class PromptTransfer:
    """One request's prompt-path traffic. All counters are as the engine reported them."""

    present: bool = False
    machine_mb_per_s: float = 0.0
    prompt_tokens: int = 0
    prompt_ms: float = 0.0
    cache_hits: int = 0
    cache_lookups: int = 0
    ram_blobs: int = 0
    file_blobs: int = 0
    file_mb: float = 0.0
    prompt_read: int = 0
    offloaded: int = 0

    # Derived. io_ms is the prompt time the file traffic accounts for at the measured rate.
    io_ms: float = 0.0
    io_share: float = 0.0
    file_mb_per_tok: float = 0.0
    cache_hit_rate: float = 0.0
    # Percentage of the routed experts served off the card. DONE's [offloaded] is explicitly
    # not inside [lookups] (#588), so the denominator is lookups + offloaded and the numerator
    # is offloaded plus the cache misses (lookups - hits).
    routed_experts: int = 0
    off_card_pct: float = 0.0
    off_card_misses: int = 0
    # Which counter the share came from: "engine-offload", "cache-miss", "both", "on-card"
    # when every routed expert was resident, or "none" when the run reported no counters.
    off_card_source: str = "none"

    @classmethod
    def from_done(cls, done: dict[str, Any], machine_mb_per_s: float | None) -> "PromptTransfer":
        """Build the record from a parsed DONE dict. Missing fields stay zero."""
        t = cls()
        try:
            t.prompt_tokens = int(done.get("prompt_tokens", 0) or 0)
            t.prompt_ms = float(done.get("prompt_ms", 0.0) or 0.0)
            t.prompt_read = int(done.get("prompt_read", 0) or 0)
            t.cache_hits = int(done.get("cache_hits", 0) or 0)
            t.cache_lookups = int(done.get("cache_lookups", 0) or 0)
            t.ram_blobs = int(done.get("ram_blobs", 0) or 0)
            t.file_blobs = int(done.get("file_blobs", 0) or 0)
            t.file_mb = float(done.get("file_mb", 0.0) or 0.0)
            t.offloaded = int(done.get("offloaded", 0) or 0)
        except (TypeError, ValueError):
            return t  # an unreadable counter means no telemetry, not a failed run
        if machine_mb_per_s and machine_mb_per_s > 0 and t.prompt_ms > 0:
            t.machine_mb_per_s = float(machine_mb_per_s)
            t.io_ms = t.file_mb / t.machine_mb_per_s * 1000.0
            t.io_share = min(1.0, max(0.0, t.io_ms / t.prompt_ms))
            t.present = t.file_mb > 0.0
        denom = t.prompt_read or t.prompt_tokens
        if denom:
            t.file_mb_per_tok = t.file_mb / denom
        if t.cache_lookups:
            t.cache_hit_rate = t.cache_hits / t.cache_lookups
        t.routed_experts = t.cache_lookups + t.offloaded
        t.off_card_misses = max(0, t.cache_lookups - t.cache_hits)
        if t.routed_experts > 0:
            t.off_card_pct = 100.0 * (t.offloaded + t.off_card_misses) / t.routed_experts
            if t.offloaded and t.off_card_misses:
                t.off_card_source = "both"
            elif t.offloaded:
                t.off_card_source = "engine-offload"
            elif t.off_card_misses:
                t.off_card_source = "cache-miss"
            else:
                t.off_card_source = "on-card"
        return t

    def as_dict(self) -> dict[str, float]:
        """The flat columns a CSV row and a history record can store."""
        return {
            "machine_mb_per_s": round(self.machine_mb_per_s, 1),
            "file_mb": round(self.file_mb, 1),
            "file_mb_per_tok": round(self.file_mb_per_tok, 4),
            "io_ms": round(self.io_ms, 1),
            "io_share": round(self.io_share, 6),
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "cache_lookups": self.cache_lookups,
            "cache_hits": self.cache_hits,
            "ram_blobs": self.ram_blobs,
            "file_blobs": self.file_blobs,
            "offloaded": self.offloaded,
            "off_card_pct": round(self.off_card_pct, 2),
        }

    def lines(self) -> list[str]:
        """Human-readable summary for the sweep report."""
        if not self.present:
            return ["  transfer: no pack read counters in this run"]
        return [
            (
                f"  transfer: {self.file_mb:.0f} MB read in {self.prompt_ms:.0f} ms prompt "
                f"({self.file_mb / (self.prompt_ms / 1000.0):.0f} MB/s achieved, "
                f"machine {self.machine_mb_per_s:.0f} MB/s)"
            ),
            (
                f"  bus share: {self.io_share:.0%} of the prompt time is file reads "
                f"({self.io_ms:.0f} ms of {self.prompt_ms:.0f} ms), "
                f"cache hits {self.cache_hit_rate:.0%} of {self.cache_lookups} lookups"
            ),
            self.off_card_line(),
        ]

    def off_card_line(self) -> str:
        """Where the decode's experts came from, and which counter said so."""
        if self.routed_experts <= 0:
            return "  off-card: no decode expert counters in this run"
        if self.off_card_pct <= 0.0:
            return (f"  off-card: 0.0% of {self.routed_experts} routed experts "
                    f"(all from VRAM)")
        parts = []
        if self.offloaded:
            parts.append(f"{self.offloaded} staged over PCIe or by another GPU (#588)")
        if self.off_card_misses:
            parts.append(f"{self.off_card_misses} not in the expert cache "
                        f"(hit rate {self.cache_hit_rate:.1%})")
        return (f"  off-card: {self.off_card_pct:.1f}% of {self.routed_experts} routed experts "
                f"[{', '.join(parts)}] via {self.off_card_source}")


def off_card_share(transfer: dict[str, Any]) -> float | None:
    """The off-card percentage of a recorded run, or None when it has no decode counters.

    A fresh `as_dict()` carries cache_hits, so the misses are exact. An older row that only
    has cache_hit_rate, or one from the token-graph path where `offloaded` is always 0, is
    rebuilt from that rate. When neither hit counter exists the misses are unknown and only
    the #588 offload count is used - guessing every lookup was a miss would invent a share.
    A run with no routed experts at all is None, not 0.0: "nothing measured" and "nothing off
    the card" are different statements.
    """
    try:
        offloaded = int(transfer.get("offloaded") or 0)
        lookups = int(transfer.get("cache_lookups") or 0)
        raw_hits = transfer.get("cache_hits")
        rate = float(transfer.get("cache_hit_rate") or 0.0)
    except (TypeError, ValueError):
        return None
    if raw_hits is not None:
        hits = int(raw_hits or 0)
    elif rate > 0.0:
        hits = round(rate * lookups)
    else:
        hits = lookups  # no hit counter at all: assume nothing is known to have missed
    routed = lookups + offloaded
    if routed <= 0:
        return None
    return round(100.0 * (offloaded + max(0, lookups - hits)) / routed, 2)


def estimated_transfer(prefill_tok_s: float, prompt_tokens: int, anchor_mb_per_s: float,
                       file_mb: float) -> dict[str, float]:
    """Transfer columns for a run that never reported them.

    History rows written before the DONE counters existed have no telemetry, and scoring
    them with the plain rate makes the balance objective degenerate into the prefill
    objective. The pack read volume for a given prompt size is a property of the pack and
    the request, not of the configuration - every one of the 75 October runs on this box
    that used a 20480-token prompt read 24963.4 MB - so io_ms follows from the anchor rate
    and prompt_ms follows from the measured prefill rate. Prompt reuse makes this an upper
    bound on prompt_ms, hence a lower bound on the bus share.
    """
    if prefill_tok_s <= 0 or prompt_tokens <= 0 or anchor_mb_per_s <= 0 or file_mb <= 0:
        return {}
    io_ms = 1000.0 * file_mb / anchor_mb_per_s
    prompt_ms = 1000.0 * prompt_tokens / prefill_tok_s
    return {
        "machine_mb_per_s": round(anchor_mb_per_s, 1),
        "file_mb": round(file_mb, 1),
        "file_mb_per_tok": round(file_mb / prompt_tokens, 4),
        "io_ms": round(io_ms, 1),
        "io_share": round(min(1.0, max(0.0, io_ms / prompt_ms)), 6),
        "cache_hit_rate": 0.0,
        "cache_lookups": 0,
        "ram_blobs": 0,
        "file_blobs": 0,
        "offloaded": 0,
        "off_card_pct": 0.0,
    }


def cross_check(engine_mb_per_s: float, requested_mb: float, disk_read_mb: float,
                window_ms: float) -> dict[str, float]:
    """Compare the engine's boot read-rate anchor with what the kernel actually moved.

    `requested_mb` is the file traffic the prompt path asked for (DONE position 13) and
    `disk_read_mb` is the delta in psutil's block-device read counters sampled around the
    same run. A ratio near 1 means the anchor is real bus traffic; a ratio far below 1
    means page cache served the reads and io_share is an upper bound. `present` is 0.0
    when either side is missing, so a caller never averages a placeholder.
    """
    requested = float(requested_mb or 0.0)
    observed = float(disk_read_mb or 0.0)
    window_s = float(window_ms or 0.0) / 1000.0
    anchor = round(float(engine_mb_per_s or 0.0), 1)
    if requested <= 0.0 or observed <= 0.0 or window_s <= 0.0:
        return {"engine_mb_per_s": anchor, "requested_mb": round(requested, 1),
                "observed_mb": round(observed, 1), "ratio": 0.0,
                "observed_mb_per_s": 0.0, "present": 0.0}
    return {
        "engine_mb_per_s": anchor,
        "requested_mb": round(requested, 1),
        "observed_mb": round(observed, 1),
        "ratio": round(observed / requested, 4),
        "observed_mb_per_s": round(observed / window_s, 1),
        "present": 1.0,
    }


def balance_score(prefill_tok_s: float, transfer: PromptTransfer | dict[str, Any] | None,
                  weight: float = TRANSFER_WEIGHT) -> float:
    """Prefill throughput discounted by how bus-bound the prompt is.

    Accepts a PromptTransfer or the flat dict stored in a trial record. No telemetry
    returns the plain rate: the discount is only ever applied to a number the engine
    measured.
    """
    if not transfer:
        return prefill_tok_s
    if isinstance(transfer, PromptTransfer):
        share = transfer.io_share
    else:
        share = float(transfer.get("io_share", 0.0) or 0.0)
    return prefill_tok_s * (1.0 - weight * min(1.0, max(0.0, share)))


def _num(row: dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _dominates(a: dict[str, Any], b: dict[str, Any], axes: tuple[str, ...]) -> bool:
    """a is at least as good on every axis and strictly better on one."""
    not_worse = all(
        (_num(a, axis) >= _num(b, axis)) if axis == "prefill_tok_s"
        else (_num(a, axis) <= _num(b, axis))
        for axis in axes
    )
    strictly = any(
        (_num(a, axis) > _num(b, axis)) if axis == "prefill_tok_s"
        else (_num(a, axis) < _num(b, axis))
        for axis in axes
    )
    return not_worse and strictly


def pareto_front(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The non-dominated trials: throughput up, bus time and bytes read down.

    A row dominated by another is worse on every axis at once, so it cannot be the
    balance point.
    """
    axes = ("prefill_tok_s", "io_ms", "file_mb")
    front: list[dict[str, Any]] = []
    for row in rows:
        if any(_dominates(other, row, axes) for other in front):
            continue
        front = [other for other in front if not _dominates(row, other, axes)]
        front.append(row)
    return front


def frontier(rows: Sequence[dict[str, Any]]) -> list[str]:
    """The non-dominated configurations as readable lines."""
    measured = [row for row in rows if _num(row, "prefill_tok_s") > 0.0]
    if not measured:
        return []
    lines: list[str] = []
    for row in sorted(pareto_front(measured), key=lambda r: -_num(r, "prefill_tok_s")):
        lines.append(
            f"  {_num(row, 'prefill_tok_s'):>8.2f} tok/s  bus {_num(row, 'io_share'):.0%}  "
            f"{_num(row, 'file_mb'):>9.0f} MB read  "
            f"{_num(row, 'file_mb') / max(1.0, _num(row, 'prompt_ms') / 1000.0):>8.0f} MB/s  "
            f"cache {_num(row, 'cache_hit_rate'):.0%}"
        )
    return lines


def best_per_resource(points: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """One rung per distinct resource value: (resource, best tok_s), increasing.

    A sweep whose sampler never leaves one vram setting yields a single rung. Building a
    ladder from raw trial rows instead of collapsing them first reports a knee from
    run-to-run noise at the same reserve, which is the false claim this guards against.
    A rung whose best rate is zero is kept: it is a measured reserve that produced no
    throughput, not a missing measurement.
    """
    top: dict[float, float] = {}
    for resource, rate in points:
        if rate is None:
            continue
        value = float(rate)
        if resource not in top or value > top[resource]:
            top[resource] = value
    return sorted(top.items())


def knee(points: Sequence[tuple[float, float]], min_gain: float = 0.03,
         stop_below: float = 0.70) -> int | None:
    """The index where spending more of a resource stops buying throughput.

    `points` is (resource, tok_s), e.g. the vram ladder (reserve MiB, prefill tok/s);
    duplicate resource values are collapsed to their best rate first. Returns the index
    into the collapsed ladder whose gain over the best so far is under `min_gain`, or
    where the rate has fallen to `stop_below` of the best. None when there is only one
    rung or the ladder never stops paying.
    """
    ladder = best_per_resource(points)
    if len(ladder) < 2:
        return None
    best = ladder[0][1]
    for i in range(1, len(ladder)):
        rate = ladder[i][1]
        if best <= 0:
            best = rate
            continue
        if (rate - best) / best < min_gain or rate < stop_below * best:
            return i
        best = rate
    return None
