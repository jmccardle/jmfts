"""PELT-based topic segmentation on embedding sequences.

Uses change-point detection from the `ruptures` library to find topic
boundaries in ordered sequences of document embeddings.  Because the
embedding service produces L2-normalised vectors, the standard "l2" cost
model is monotonically equivalent to cosine distance:
    ||a - b||² = 2 − 2·cos(a, b)
"""

from dataclasses import dataclass

import numpy as np
import ruptures


@dataclass
class Segment:
    """A contiguous segment of child documents sharing a topic."""

    start: int  # inclusive index into the child list
    end: int  # exclusive index
    child_ids: list[int]  # document IDs in this segment


def pelt_segment(
    embeddings: np.ndarray,
    child_ids: list[int],
    penalty: float = 1.0,
    min_size: int = 2,
    jump: int = 5,
) -> list[Segment]:
    """Run PELT change-point detection on an ordered embedding sequence.

    Args:
        embeddings: (n, dim) array of L2-normalised embeddings.
        child_ids: Parallel list of document IDs (same length as embeddings).
        penalty: BIC-style penalty — higher ⇒ fewer breakpoints (coarser segments).
        min_size: Minimum segment length (ruptures constraint).
        jump: Grid resolution — ``ruptures`` only considers breakpoints at multiples of
            this. It was previously left at ``ruptures``' own default of 5 and never
            named here, which is a silent resolution limit worth stating: with ``jump=5``
            a sequence of six items has exactly one candidate breakpoint, and if that
            candidate violates ``min_size`` the sequence comes back as one segment no
            matter how sharp the change in it is. ``jump=1`` considers every position and
            costs O(n²) instead of O(n²/jump). The default is unchanged so existing
            callers behave as they did; ``jmfts_core.rollup_tasks`` passes 1.

    Returns:
        List of Segment objects covering the full sequence.

    Raises:
        ValueError: If inputs are empty or mismatched.
    """
    n = len(embeddings)
    if n == 0:
        raise ValueError("Cannot segment an empty embedding sequence")
    if len(child_ids) != n:
        raise ValueError(f"Length mismatch: {n} embeddings vs {len(child_ids)} child_ids")
    if n < min_size:
        # Too few documents to detect a change point — return one segment
        return [Segment(start=0, end=n, child_ids=list(child_ids))]

    algo = ruptures.Pelt(model="l2", min_size=min_size, jump=jump).fit(embeddings)
    breakpoints = algo.predict(pen=penalty)
    # breakpoints is e.g. [5, 12, 20] where 20 == n (always ends with n)

    segments: list[Segment] = []
    prev = 0
    for bp in breakpoints:
        segments.append(Segment(start=prev, end=bp, child_ids=list(child_ids[prev:bp])))
        prev = bp

    return segments


def enforce_segment_bounds(
    segments: list[Segment],
    min_segment: int = 3,
    max_segment: int = 10,
) -> list[Segment]:
    """Post-process PELT segments to enforce min/max size constraints.

    Args:
        segments: Output from pelt_segment().
        min_segment: Merge segments smaller than this into a neighbour.
        max_segment: Split segments larger than this into roughly equal parts.

    Returns:
        New list of Segment objects with adjusted boundaries.
    """
    if not segments:
        return segments

    # --- Phase 1: merge undersized segments into nearest neighbour ----------
    merged = list(segments)
    changed = True
    while changed and len(merged) > 1:
        changed = False
        for i, s in enumerate(merged):
            if s.end - s.start >= min_segment:
                continue
            # Pick neighbour to merge with (prefer the smaller one)
            if i == 0:
                j = 1
            elif i == len(merged) - 1:
                j = i - 1
            else:
                left_sz = merged[i - 1].end - merged[i - 1].start
                right_sz = merged[i + 1].end - merged[i + 1].start
                j = i - 1 if left_sz <= right_sz else i + 1

            a, b = min(i, j), max(i, j)
            combined = Segment(
                start=merged[a].start,
                end=merged[b].end,
                child_ids=merged[a].child_ids + merged[b].child_ids,
            )
            merged = merged[:a] + [combined] + merged[b + 1 :]
            changed = True
            break  # restart scan after mutation

    # --- Phase 2: split oversized segments ----------------------------------
    result: list[Segment] = []
    for s in merged:
        size = s.end - s.start
        if size <= max_segment:
            result.append(s)
            continue
        num_parts = (size + max_segment - 1) // max_segment
        part_size = size // num_parts
        remainder = size % num_parts
        offset = 0
        for p in range(num_parts):
            chunk = part_size + (1 if p < remainder else 0)
            result.append(
                Segment(
                    start=s.start + offset,
                    end=s.start + offset + chunk,
                    child_ids=s.child_ids[offset : offset + chunk],
                )
            )
            offset += chunk

    return result
