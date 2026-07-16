#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Syntra installer — an awesome, animated install experience.
#
#  Usage:
#     ./install.sh                                      # from a source checkout
#     ./install.sh --method uv|pipx|pip                 # force an installer
#     ./install.sh --dry-run                            # show the flow, install nothing
#     ./install.sh --no-anim                            # skip animations (fast/CI)
#
#  Honors NO_COLOR and non-TTY output (degrades to plain text). Pure bash + ANSI;
#  no dependencies of its own. Detects uv → pipx → pip and installs from PyPI when
#  published, else from the local source tree.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ───────────────────────────── config / flags ──────────────────────────────
PKG="syntra"
MIN_PY_MAJOR=3
MIN_PY_MINOR=10
METHOD=""
DRY_RUN=0
NO_ANIM=0
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

for arg in "$@"; do
  case "$arg" in
    --method=*) METHOD="${arg#*=}" ;;
    --method)   shift; METHOD="${1:-}" ;;
    --dry-run)  DRY_RUN=1 ;;
    --no-anim)  NO_ANIM=1 ;;
    -h|--help)
      sed -n '2,18p' "${BASH_SOURCE[0]:-$0}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
  esac
done

# ───────────────────────── color / tty capability ──────────────────────────
USE_COLOR=1
{ [ -n "${NO_COLOR:-}" ] || [ ! -t 1 ]; } && USE_COLOR=0
{ [ ! -t 1 ] ; } && NO_ANIM=1     # never animate when piped/redirected

# 24-bit truecolor if available, else degrade. We use raw SGR for the gradient.
esc() { [ "$USE_COLOR" = 1 ] && printf '\033[%sm' "$1" || true; }
rgb() { [ "$USE_COLOR" = 1 ] && printf '\033[38;2;%d;%d;%dm' "$1" "$2" "$3" || true; }
RESET="$(esc 0)"; BOLD="$(esc 1)"; DIM="$(esc 2)"
GREEN="$(rgb 80 220 130)"; RED="$(rgb 240 90 90)"; YELLOW="$(rgb 240 200 90)"
GREY="$(rgb 130 140 160)"; ACCENT="$(rgb 90 180 230)"
hide_cursor() { [ "$NO_ANIM" = 1 ] || { [ "$USE_COLOR" = 1 ] && printf '\033[?25l'; }; }
show_cursor() { [ "$USE_COLOR" = 1 ] && printf '\033[?25h' || true; }
trap 'show_cursor' EXIT INT TERM

# Brand gradient: a stop list (blue → cyan → violet) we interpolate across.
# Returns "R G B" for t in [0,1].
grad() { # $1 = t (0..1000)
  local t=$1 segs=3 seg pos
  # stops: (60,150,230) blue, (70,210,210) cyan, (170,110,235) violet
  local r0=60 g0=150 b0=230  r1=70 g1=210 b1=210  r2=170 g2=110 b2=235
  if [ "$t" -lt 500 ]; then
    pos=$(( t * 2 )); R=$(( r0 + (r1-r0)*pos/1000 )); G=$(( g0 + (g1-g0)*pos/1000 )); B=$(( b0 + (b1-b0)*pos/1000 ))
  else
    pos=$(( (t-500) * 2 )); R=$(( r1 + (r2-r1)*pos/1000 )); G=$(( g1 + (g2-g1)*pos/1000 )); B=$(( b1 + (b2-b1)*pos/1000 ))
  fi
}

# ───────────────────────────── the logo art ────────────────────────────────
LOGO=(
'  ███████╗██╗   ██╗███╗   ██╗████████╗██████╗  █████╗ '
'  ██╔════╝╚██╗ ██╔╝████╗  ██║╚══██╔══╝██╔══██╗██╔══██╗'
'  ███████╗ ╚████╔╝ ██╔██╗ ██║   ██║   ██████╔╝███████║'
'  ╚════██║  ╚██╔╝  ██║╚██╗██║   ██║   ██╔══██╗██╔══██║'
'  ███████║   ██║   ██║ ╚████║   ██║   ██║  ██║██║  ██║'
'  ╚══════╝   ╚═╝   ╚═╝  ╚═══╝   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝'
)
TAGLINE="the multi-model coordinator — best model per role, every step"

# Print the logo with a horizontal gradient. If animate, sweep a bright band.
render_logo() { # $1 = phase shift (0..1000) for the sweep; absent = static gradient
  local shift="${1:-}"
  local row col line len R G B t bright
  for row in "${!LOGO[@]}"; do
    line="${LOGO[$row]}"; len=${#line}
    for (( col=0; col<len; col++ )); do
      local ch="${line:col:1}"
      if [ "$ch" = " " ]; then printf ' '; continue; fi
      t=$(( col * 1000 / (len>0?len:1) ))
      grad "$t"
      if [ -n "$shift" ]; then
        # a moving highlight band brightens chars near the shift position
        local d=$(( col*1000/len - shift )); d=${d#-}
        if [ "$d" -lt 90 ]; then R=$((R+90>255?255:R+90)); G=$((G+90>255?255:G+90)); B=$((B+90>255?255:B+90)); fi
      fi
      [ "$USE_COLOR" = 1 ] && printf '\033[1;38;2;%d;%d;%dm%s' "$R" "$G" "$B" "$ch" || printf '%s' "$ch"
    done
    printf '%s\n' "$RESET"
  done
}

animate_logo() {
  if [ "$NO_ANIM" = 1 ]; then render_logo; return; fi
  local s
  for s in 0 140 280 420 560 700 840 1000; do
    printf '\033[%dA' "${#LOGO[@]}"   # cursor up to repaint
    render_logo "$s"
    sleep 0.05
  done
  printf '\033[%dA' "${#LOGO[@]}"; render_logo   # settle to static gradient
}

# ──────────────────────────── spinner helper ───────────────────────────────
SPIN=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
# run a command with a live braille spinner + label; capture output to a log.
spin_run() { # $1 = label ; rest = command
  local label="$1"; shift
  local logf; logf="$(mktemp)"
  if [ "$NO_ANIM" = 1 ] || [ "$DRY_RUN" = 1 ]; then
    printf '   %s%s%s %s\n' "$ACCENT" '➤' "$RESET" "$label"
    [ "$DRY_RUN" = 1 ] && { printf '       %s(dry-run: %s)%s\n' "$DIM" "$*" "$RESET"; return 0; }
    if "$@" >"$logf" 2>&1; then printf '   %s✓%s %s\n' "$GREEN" "$RESET" "$label"; rm -f "$logf"; return 0
    else printf '   %s✗%s %s\n' "$RED" "$RESET" "$label"; sed 's/^/       /' "$logf"; rm -f "$logf"; return 1; fi
  fi
  "$@" >"$logf" 2>&1 &
  local pid=$! i=0 start=$SECONDS
  hide_cursor
  while kill -0 "$pid" 2>/dev/null; do
    printf '\r   %s%s%s %s %s(%ds)%s ' "$ACCENT" "${SPIN[i%10]}" "$RESET" "$label" "$DIM" "$((SECONDS-start))" "$RESET"
    i=$((i+1)); sleep 0.08
  done
  show_cursor
  if wait "$pid"; then
    printf '\r   %s✓%s %s%*s\n' "$GREEN" "$RESET" "$label" 14 ' '; rm -f "$logf"; return 0
  else
    printf '\r   %s✗%s %s\n' "$RED" "$RESET" "$label"; sed 's/^/       /' "$logf"; rm -f "$logf"; return 1
  fi
}

step()  { printf '   %s%s%s %s\n' "$GREEN" '✓' "$RESET" "$1"; }
warn()  { printf '   %s%s%s %s\n' "$YELLOW" '!' "$RESET" "$1"; }
fail()  { printf '   %s%s%s %s\n' "$RED" '✗' "$RESET" "$1"; }
rule()  { printf '%s   ────────────────────────────────────────────────────────%s\n' "$GREY" "$RESET"; }

# ───────────────────────────── 1. banner ───────────────────────────────────
printf '\n'
if [ "$NO_ANIM" = 1 ]; then
  render_logo                                   # plain one-shot (piped / --no-anim)
else
  for _ in "${LOGO[@]}"; do printf '\n'; done   # reserve lines, then repaint in place
  printf '\033[%dA' "${#LOGO[@]}"
  animate_logo
fi
printf '   %s%s%s\n\n' "$DIM" "$TAGLINE" "$RESET"

# ───────────────────────── 2. environment scan ─────────────────────────────
printf '%s   SCANNING ENVIRONMENT%s\n' "$BOLD" "$RESET"
OS="$(uname -s 2>/dev/null || echo unknown)"; ARCH="$(uname -m 2>/dev/null || echo ?)"
step "platform: $OS/$ARCH"

PYBIN=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then PYBIN="$cand"; break; fi
done
if [ -z "$PYBIN" ]; then
  fail "Python not found. Install Python $MIN_PY_MAJOR.$MIN_PY_MINOR+ and re-run."
  exit 1
fi
PYVER="$("$PYBIN" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"
PYOK="$("$PYBIN" -c "import sys;print(1 if sys.version_info[:2]>=($MIN_PY_MAJOR,$MIN_PY_MINOR) else 0)")"
if [ "$PYOK" = 1 ]; then step "python: $PYVER ($PYBIN)"; else
  fail "python $PYVER is too old — need $MIN_PY_MAJOR.$MIN_PY_MINOR+"; exit 1; fi

# choose installer: explicit > uv > pipx > pip(--user)
have() { command -v "$1" >/dev/null 2>&1; }
if [ -z "$METHOD" ]; then
  if have uv; then METHOD=uv; elif have pipx; then METHOD=pipx; else METHOD=pip; fi
fi
case "$METHOD" in
  uv)   have uv   || { fail "uv not found"; exit 1; }; step "installer: uv tool (isolated, fast)" ;;
  pipx) have pipx || { fail "pipx not found"; exit 1; }; step "installer: pipx (isolated)" ;;
  pip)  step "installer: pip --user"; warn "tip: 'uv' or 'pipx' give a cleaner isolated install" ;;
  *)    fail "unknown --method '$METHOD' (use uv|pipx|pip)"; exit 1 ;;
esac

# choose source: PyPI if the package is published, else the local checkout.
SRC="$PKG"; SRC_LABEL="PyPI ($PKG)"
if [ -f "$SELF_DIR/pyproject.toml" ] && grep -q "^name *= *\"$PKG\"" "$SELF_DIR/pyproject.toml" 2>/dev/null; then
  SRC="$SELF_DIR"; SRC_LABEL="local source ($SELF_DIR)"
fi
step "source: $SRC_LABEL"

# detect an existing install → update vs fresh
ACTION="install"
if have "$PKG"; then ACTION="update"; warn "existing $PKG found — will update it"; fi
printf '\n'

# ───────────────────────── 3. install / update ─────────────────────────────
HEADING="INSTALLING"; [ "$ACTION" = update ] && HEADING="UPDATING"
printf '%s   %s SYNTRA%s\n' "$BOLD" "$HEADING" "$RESET"
case "$METHOD" in
  uv)   if [ "$ACTION" = update ] && [ "$SRC" = "$PKG" ]; then CMD=(uv tool upgrade "$PKG");
        else CMD=(uv tool install --force "$SRC"); fi ;;
  pipx) if [ "$ACTION" = update ] && [ "$SRC" = "$PKG" ]; then CMD=(pipx upgrade "$PKG");
        else CMD=(pipx install --force "$SRC"); fi ;;
  pip)  CMD=("$PYBIN" -m pip install --user --upgrade "$SRC") ;;
esac
spin_run "$ACTION $PKG via $METHOD" "${CMD[@]}"

# ───────────────────────────── 4. verify ───────────────────────────────────
printf '\n%s   VERIFYING%s\n' "$BOLD" "$RESET"
if [ "$DRY_RUN" = 1 ]; then
  step "dry-run — skipped verification"
elif have "$PKG"; then
  VER="$("$PKG" --version 2>/dev/null || echo '?')"
  step "$PKG on PATH ($VER)"
else
  warn "$PKG not on PATH yet — you may need to restart your shell"
  case "$METHOD" in
    uv)   warn "or run: uv tool update-shell" ;;
    pipx) warn "or run: pipx ensurepath" ;;
    pip)  warn "add your user bin to PATH (e.g. ~/.local/bin)" ;;
  esac
fi

# ───────────────────────── 5. success panel ────────────────────────────────
printf '\n'
[ "$USE_COLOR" = 1 ] && printf '%s' "$(rgb 80 220 130)"
cat <<'PANEL'
   ╔══════════════════════════════════════════════════════════╗
   ║   ✦  Syntra is ready                                      ║
   ╚══════════════════════════════════════════════════════════╝
PANEL
printf '%s' "$RESET"
printf '   %sNext:%s\n' "$BOLD" "$RESET"
printf '     %s▸%s syntra            %s# launch the cockpit (animated first-run setup)%s\n' "$ACCENT" "$RESET" "$DIM" "$RESET"
printf '     %s▸%s syntra doctor     %s# check providers + routing health%s\n' "$ACCENT" "$RESET" "$DIM" "$RESET"
printf '     %s▸%s syntra update     %s# upgrade later, with consent%s\n' "$ACCENT" "$RESET" "$DIM" "$RESET"
printf '\n'
