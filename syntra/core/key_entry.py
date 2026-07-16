"""API-key entry modal — an interactive popup form, NOT a slash command with the key typed
inline (which would leak the secret into the input + chat history).

Triggered by `/key` (bare): a centered box the user fills in like a form —
  Provider   : openrouter
  API key    : •••••••••••••••• (masked; Ctrl+R reveals)
  Base URL   : https://openrouter.ai/api/v1   (optional — known providers fill it in)
Tab / ↑↓ move between fields, Enter saves, Esc cancels. The key is masked while typing and
never echoed back. Mirrors the reference TUIs' provider → masked-input → persist pattern.

PURE (no curses): a small state machine + a render function. The curses layer paints key_box()
and forwards keys. Unit-tested. Mirrors plan_approval.py / question_wizard.py's contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Field order in the form. (key index 1 is the masked one.)
_FIELDS = ("provider", "key", "base_url")
_LABELS = {"provider": "Provider", "key": "API key", "base_url": "Base URL"}

# Known providers → their default base_url, so picking one fills the URL in (the user only has
# to paste the key). Data-driven; extend freely. Unknown providers just need a base_url typed.
_KNOWN_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "deepseek": "https://api.deepseek.com",
    "groq": "https://api.groq.com/openai/v1",
    "xai": "https://api.x.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "together": "https://api.together.xyz/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}


@dataclass
class KeyEntryForm:
    """State for the API-key entry popup.

    `provider`/`key`/`base_url` are the three editable fields. `focus` is the field index
    (0..2) the cursor is on. `reveal` toggles masking of the key. `mode` ∈ {edit, done,
    cancelled}; on submit `result_value` = (provider, key, base_url)."""

    provider: str = ""
    key: str = ""
    base_url: str = ""
    focus: int = 0
    cursor: int = 0                                   # caret position within the focused field
    reveal: bool = False
    mode: str = "edit"                                # edit | done | cancelled
    error: str = ""                                   # inline validation message
    result_value: tuple | None = None
    _click_rows: dict = field(default_factory=dict)   # render-row -> field index
    _screen_y0: int = -1

    # ---- field access ------------------------------------------------------
    def _name(self) -> str:
        return _FIELDS[self.focus]

    def _get(self, name: str) -> str:
        return {"provider": self.provider, "key": self.key, "base_url": self.base_url}[name]

    def _set(self, name: str, val: str) -> None:
        setattr(self, name, val)

    # ---- navigation --------------------------------------------------------
    def move(self, delta: int) -> None:
        """Tab / ↑↓ between the three fields (wraps). Cursor jumps to the end of the new field."""
        if self.mode != "edit":
            return
        self.focus = (self.focus + delta) % len(_FIELDS)
        self.cursor = len(self._get(self._name()))   # land at end of the field
        self.error = ""

    def focus_field(self, i: int) -> None:
        if self.mode == "edit" and 0 <= i < len(_FIELDS):
            self.focus = i
            self.cursor = len(self._get(self._name()))
            self.error = ""

    def click_row(self, content_row: int):
        """Mouse click on a rendered field row → focus that field. Returns None (no submit)."""
        i = self._click_rows.get(int(content_row))
        if i is not None and 0 <= i < len(_FIELDS):
            self.focus = i
            self.cursor = len(self._get(self._name()))
            self.error = ""
        return None

    # ---- cursor within the focused field -----------------------------------
    def _clamp_cursor(self) -> None:
        self.cursor = max(0, min(self.cursor, len(self._get(self._name()))))

    def left(self) -> None:
        if self.mode == "edit":
            self._clamp_cursor(); self.cursor = max(0, self.cursor - 1)

    def right(self) -> None:
        if self.mode == "edit":
            self._clamp_cursor(); self.cursor = min(len(self._get(self._name())), self.cursor + 1)

    def home(self) -> None:
        if self.mode == "edit":
            self.cursor = 0

    def end(self) -> None:
        if self.mode == "edit":
            self.cursor = len(self._get(self._name()))

    # ---- editing (insert/delete AT the cursor) -----------------------------
    def type_char(self, ch: str) -> None:
        if self.mode != "edit" or not ch:
            return
        if ch == " " or ord(ch[0]) >= 32:
            name = self._name()
            cur = self._get(name)
            self._clamp_cursor()
            self._set(name, cur[:self.cursor] + ch + cur[self.cursor:])
            self.cursor += len(ch)
            self.error = ""

    def paste(self, s: str) -> None:
        """Insert a whole pasted string at the cursor. Strips CR/LF/control chars — a pasted API
        key usually carries a trailing newline, which must NEVER submit or leak a control char into
        the secret. A paste of only control chars is a no-op. Never changes mode (stays 'edit')."""
        if self.mode != "edit" or not s:
            return
        clean = "".join(c for c in s if c == " " or (ord(c) >= 32 and c not in ("\x7f",)))
        if not clean:
            return
        name = self._name()
        cur = self._get(name)
        self._clamp_cursor()
        self._set(name, cur[:self.cursor] + clean + cur[self.cursor:])
        self.cursor += len(clean)
        self.error = ""

    def backspace(self) -> None:
        """Delete the char BEFORE the cursor (not just the last char), like a real input."""
        if self.mode != "edit":
            return
        name = self._name()
        cur = self._get(name)
        self._clamp_cursor()
        if self.cursor > 0:
            self._set(name, cur[:self.cursor - 1] + cur[self.cursor:])
            self.cursor -= 1
        self.error = ""

    def toggle_reveal(self) -> None:
        """Ctrl+R: show/hide the API key while typing it."""
        self.reveal = not self.reveal

    def autofill_base_url(self) -> None:
        """When a KNOWN provider is entered and base_url is still blank, fill the default URL
        so the user only has to paste the key. Called on field-change / before submit."""
        p = self.provider.strip().lower()
        if p in _KNOWN_BASE_URLS and not self.base_url.strip():
            self.base_url = _KNOWN_BASE_URLS[p]

    # ---- submit / cancel ---------------------------------------------------
    def submit(self):
        """Validate + finish. Returns ("save", (provider, key, base_url)) on success, or None
        (and sets self.error) when a required field is missing — the modal stays open."""
        if self.mode != "edit":
            return None
        self.autofill_base_url()
        prov = self.provider.strip()
        key = self.key.strip()
        if not prov:
            self.error = "provider is required"; self.focus = 0; return None
        if not key:
            self.error = "API key is required"; self.focus = 1; return None
        self.mode = "done"
        self.result_value = (prov, key, self.base_url.strip())
        return ("save", self.result_value)

    def cancel(self) -> None:
        self.mode = "cancelled"

    def result(self):
        return self.result_value if self.mode == "done" else None


# ---- render ----------------------------------------------------------------

def _masked(key: str, reveal: bool) -> str:
    """The key as shown: full when reveal, else bullets (last 4 visible once it's long enough so
    the user can sanity-check the paste without exposing the whole secret)."""
    if reveal:
        return key
    n = len(key)
    if n == 0:
        return ""
    if n <= 8:
        return "•" * n
    return "•" * (n - 4) + key[-4:]


def key_box(form: KeyEntryForm, width: int) -> list:
    """Render the form -> [(text, style)]. Pure; the caller centers + borders it. Records
    _click_rows so a mouse click on a field focuses it."""
    w = max(34, int(width))
    out: list = []
    form._click_rows = {}

    out.append(("  Add an API key", "accent"))
    out.append(("  the key is saved (chmod 600) and never shown in chat", "dim"))
    out.append(("", "default"))

    label_w = max(len(_LABELS[f]) for f in _FIELDS)
    for i, name in enumerate(_FIELDS):
        focused = (i == form.focus and form.mode == "edit")
        cur = "❯ " if focused else "  "
        label = _LABELS[name].ljust(label_w)
        if name == "key":
            shown = _masked(form.key, form.reveal)
            opt = "  (Ctrl+R reveal)" if focused else ""
        elif name == "base_url":
            shown = form.base_url
            opt = "  (optional)" if (focused and not form.base_url) else ""
        else:
            shown = form.provider
            opt = ""
        # Show the caret AT the cursor position in the focused field (so mid-field editing is
        # visible), not just parked at the end. Masking is applied first, then the caret is
        # inserted at the same index (masked + real strings are the same length up to the last-4).
        if focused:
            cpos = max(0, min(form.cursor, len(shown)))
            shown = shown[:cpos] + "▏" + shown[cpos:]
        line = f"  {cur}{label} : {shown}{opt}"
        form._click_rows[len(out)] = i
        out.append((line[:w], "user" if focused else "default"))

    out.append(("", "default"))
    if form.error:
        out.append((f"  ⚠ {form.error}", "diff_del"))
    out.append(("  Tab/↑↓ move · Enter save · Esc cancel", "dim"))
    return out
