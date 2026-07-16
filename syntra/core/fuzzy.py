"""Fuzzy matching for the pickers (command palette, model/file/skill search).

Syntra's own subsequence scorer. A query matches a candidate when its characters
appear in order (not necessarily adjacent); among all such orderings we score the
single best one and return where it landed (so the UI can highlight it).

Design, from first principles:

* **Alignment.** We score the best in-order placement with a local-alignment dynamic
  program with affine gap costs — the standard Smith-Waterman (1981) recurrence with
  Gotoh (1982) two-state gaps (a textbook method; the weights below are ours). Two
  rolling rows per query character: ``hit`` = best score that PLACES the current query
  char here, ``run`` = best score that SKIPS here through an open gap. A first skip
  costs more than each continued skip, so one long gap beats many scattered ones.

* **Position worth, by character role.** Each landing spot earns a base reward plus a
  role bonus we derive from the *transition* between the previous and current character,
  classified via Unicode general categories (``unicodedata.category``) rather than a
  hand-listed punctuation set — so any separator (ASCII or not) counts. Roles, strongest
  first: the very first character, a character right after a separator, a camel hump
  (lower→Upper), and a script change (letters↔digits). Adjacent placements (a tight run)
  get a streak reward on top.

* **Reordered runs.** A query whose letter/digit runs arrive in a different order than
  the candidate (``ab12cd`` vs ``cd-12-ab``, ``v4ds`` vs ``deepseek-v4``) still matches:
  we split both into maximal same-class runs and accept the query when each of its runs
  can be assigned to a distinct candidate run it is a subsequence of (a small bipartite
  matching). This generalises the common two-part letters↔digits swap to any number of
  runs and needs no regex.

Higher score = better; an empty query scores 0. Pure + deterministic, unit-tested.
The tables are O(len(query) × len(candidate)); candidates here are short.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# ---- scoring weights (ours; a small-integer scale, no derived relationships) --------
_HIT = 10.0            # base reward for landing a query char anywhere
# role bonuses, by the transition into the landed character (strongest first)
_ROLE_FIRST = 9.0      # the first character of the candidate
_ROLE_AFTER_SEP = 7.0  # immediately after a separator / whitespace
_ROLE_CAMEL = 6.0      # a camelCase hump: lowercase -> uppercase
_ROLE_SCRIPT = 5.0     # a letter<->digit script change
_STREAK = 7.0          # adjacent to the previous landed char (a tight run)
_SKIP_FIRST = 2.0      # cost to open a gap (first skipped char)
_SKIP_MORE = 0.5       # cost per additional skipped char
_DRIFT = 0.25          # mild nudge toward earlier landings
_WHOLE = 90.0          # the query equals the whole candidate
_REORDER_PENALTY = 6.0 # ranked below an in-order match when only run-reorder matches
_WORST = float("-inf")

# character roles for boundary detection, from Unicode general category + case
_R_OTHER = 0   # separators / punctuation / whitespace (everything not below)
_R_LOWER = 1
_R_UPPER = 2
_R_DIGIT = 3
_R_LETTER = 4  # a cased-less or other letter (CJK, etc.)


def _role(ch: str) -> int:
    """Classify one character by Unicode general category (+ case), so boundary logic
    is derived from the standard's categories, not a hardcoded punctuation literal."""
    cat = unicodedata.category(ch)      # e.g. 'Ll', 'Lu', 'Nd', 'Pd', 'Zs'
    if cat == "Ll":
        return _R_LOWER
    if cat in ("Lu", "Lt"):
        return _R_UPPER
    if cat[0] == "N":                   # Nd / Nl / No
        return _R_DIGIT
    if cat[0] == "L":                   # Lm / Lo and other letters
        return _R_LETTER
    return _R_OTHER                     # P*, S*, Z*, C* -> separator-like


@dataclass(frozen=True)
class Match:
    text: str
    score: float                 # HIGHER = better
    positions: tuple[int, ...]   # matched character indices in `text` (for highlight)


def _position_worth(orig: str, roles: list, i: int) -> float:
    """Base reward plus a role bonus for landing a query char at index ``i`` of the
    candidate, read from the ORIGINAL-case text via the precomputed ``roles``."""
    base = _HIT - _DRIFT * i
    if i == 0:
        return base + _ROLE_FIRST
    prev_r, cur_r = roles[i - 1], roles[i]
    if prev_r == _R_OTHER and cur_r != _R_OTHER:
        return base + _ROLE_AFTER_SEP
    if prev_r == _R_LOWER and cur_r == _R_UPPER:
        return base + _ROLE_CAMEL
    if (cur_r == _R_DIGIT) != (prev_r == _R_DIGIT):
        return base + _ROLE_SCRIPT
    return base


def _best_alignment(q: str, cand_lower: str, orig: str):
    """Best in-order placement of every char of ``q`` within ``cand_lower``. Returns
    ``(score, positions)`` or ``(None, None)`` when ``q`` is not a subsequence. ``orig``
    carries original case for role detection (the scan runs on a lowercased copy)."""
    m, n = len(q), len(cand_lower)
    if m == 0:
        return 0.0, []
    if m > n:
        return None, None

    roles = [_role(c) for c in orig]
    worth = [_position_worth(orig, roles, i) for i in range(n)]

    # `hit[i]`  = best score that PLACES the current query char at candidate index i.
    # `back[k]` = for query char k at column i, the column of char k-1 (for backtrack).
    hit = [_WORST] * n
    back = [[-1] * n for _ in range(m)]

    # first query character: may land on any matching column, no predecessor.
    first = q[0]
    for i in range(n):
        if cand_lower[i] == first:
            hit[i] = worth[i]

    for k in range(1, m):
        qc = q[k]
        prev = hit
        cur = [_WORST] * n
        # `run` carries the best "skip through an open gap" value as we sweep left->right.
        run = _WORST           # best score to arrive at column i via >=1 skipped chars
        run_src = -1           # column of char k-1 that opened that gap
        for i in range(n):
            # advance the open-gap frontier to include placing char k-1 two cols back,
            # or extending a gap already in progress (affine: open costs more than extend)
            if i >= 2 and prev[i - 2] > _WORST:
                opened = prev[i - 2] - _SKIP_FIRST
                extended = run - _SKIP_MORE if run > _WORST else _WORST
                if opened >= extended:
                    run, run_src = opened, i - 2
                else:
                    run = extended
            elif run > _WORST:
                run = run - _SKIP_MORE
            if cand_lower[i] != qc:
                continue
            # place char k here: either adjacent to char k-1 (a streak) or after a gap
            cand_score, src = _WORST, -1
            if i >= 1 and prev[i - 1] > _WORST:
                streak = prev[i - 1] + _STREAK
                if streak > cand_score:
                    cand_score, src = streak, i - 1
            if run > _WORST and run > cand_score:
                cand_score, src = run, run_src
            if cand_score > _WORST:
                cur[i] = cand_score + worth[i]
                back[k][i] = src
        hit = cur

    end, best = -1, _WORST
    for i in range(n):
        if hit[i] > best:
            best, end = hit[i], i
    if end < 0:
        return None, None

    positions = []
    k, i = m - 1, end
    while k >= 0 and i >= 0:
        positions.append(i)
        i = back[k][i]
        k -= 1
    positions.reverse()
    if q == cand_lower:
        best += _WHOLE
    return best, positions


def _runs(s: str) -> list:
    """Split a string into maximal same-class runs, dropping separators. Returns a list
    of ``(role, text)`` where role is letters (_R_LETTER) or digits (_R_DIGIT); used for
    order-insensitive run matching. A single left-to-right scan, no regex."""
    out: list = []
    cur_kind, start = None, 0
    for i, ch in enumerate(s):
        r = _role(ch)
        kind = _R_DIGIT if r == _R_DIGIT else (_R_LETTER if r != _R_OTHER else None)
        if kind != cur_kind:
            if cur_kind is not None:
                out.append((cur_kind, s[start:i]))
            cur_kind, start = kind, i
    if cur_kind is not None:
        out.append((cur_kind, s[start:]))
    return out


def _is_subseq(needle: str, hay: str) -> bool:
    """True if every char of ``needle`` appears in ``hay`` in order (greedy two-pointer)."""
    it = iter(hay)
    return all(c in it for c in needle)


# cap on query runs we'll permute — keeps the reorder fallback cheap (real queries are short)
_MAX_REORDER_RUNS = 5


def _reordered_candidate(q_runs: list, t_lower: str) -> str | None:
    """If some ORDERING of the query's runs, concatenated, is a subsequence of the
    candidate, return that concatenation (to be aligned + penalised). This generalises
    the common two-part letters/digits swap to any number of runs, with no regex: we
    simply look for a run permutation the candidate can satisfy in order. Returns the
    first such ordering, or None."""
    from itertools import permutations
    texts = [t for _k, t in q_runs]
    if not (2 <= len(texts) <= _MAX_REORDER_RUNS):
        return None
    for perm in permutations(texts):
        joined = "".join(perm)
        if _is_subseq(joined, t_lower):
            return joined
    return None


def fuzzy_score(query: str, text: str) -> Match | None:
    """Best-alignment fuzzy match of ``query`` against ``text``. Returns a Match
    (higher score = better) or None when there is no match."""
    if query == "":
        return Match(text=text, score=0.0, positions=())
    q_lower, t_lower = query.lower(), text.lower()
    score, positions = _best_alignment(q_lower, t_lower, text)
    if score is not None:
        return Match(text=text, score=score, positions=tuple(positions))

    # fall back to order-insensitive run matching: the query's letter/digit runs may
    # appear in the candidate in a different order (e.g. "codex52" vs "5.2-codex",
    # "ab12cd" vs "cd-12-ab"). Find an ordering the candidate satisfies, align it, and
    # rank it just below any genuine in-order match.
    reordered = _reordered_candidate(_runs(q_lower), t_lower)
    if reordered is not None:
        s2, p2 = _best_alignment(reordered, t_lower, text)
        if s2 is not None:
            return Match(text=text, score=s2 - _REORDER_PENALTY, positions=tuple(p2))
    return None


def fuzzy_filter(query: str, candidates) -> list[Match]:
    """Keep and rank the candidates that fuzzy-match ``query``; best first, stable on
    ties. The query is split on whitespace into words and EVERY word must match (the
    returned score sums the words; highlight positions come from the first word)."""
    words = query.split()
    if not words:
        return [Match(text=c, score=0.0, positions=()) for c in candidates]

    ranked: list[tuple[float, int, Match]] = []
    for idx, cand in enumerate(candidates):
        summed = 0.0
        head_positions: tuple[int, ...] = ()
        ok = True
        for w_i, word in enumerate(words):
            hit = fuzzy_score(word, cand)
            if hit is None:
                ok = False
                break
            summed += hit.score
            if w_i == 0:
                head_positions = hit.positions
        if ok:
            ranked.append((-summed, idx, Match(text=cand, score=summed,
                                               positions=head_positions)))

    ranked.sort(key=lambda r: (r[0], r[1]))   # best score first, original order on ties
    return [r[2] for r in ranked]
