# Animations

A small, self-contained terminal animation demo used by Syntra.

## `pacman_compaction.py`
A clean, iconic Pac-Man animation: Pac chomps a row of pellets (representing messages),
eats a power-pellet, and collapses everything into a compact `memory.pkg` block. 24-bit
truecolor, standard-library only.

```bash
python3 docs/animations/pacman_compaction.py         # play once
python3 docs/animations/pacman_compaction.py loop    # loop
```

## Design note
Syntra's context compaction is effectively instant (deterministic, no model call), so there
is no real duration for an animation to fill. An animation should therefore either play as a
**one-shot stamp** (plays once and stops) for an instantaneous event, or be **gated on
genuinely slow work** (a model turn, a batch run) so it ends exactly when the work ends —
never loop blindly to pad time.
