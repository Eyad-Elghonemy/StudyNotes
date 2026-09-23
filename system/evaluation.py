"""
Feedback / evaluation module for StudyNotes.

Lets the user rate the app and send suggestions + problems they found. The
message goes to a Supabase table through its plain REST API (urllib from the
standard library only - no `supabase` package, so the .exe doesn't grow).

Design notes
------------
* Only the public `anon` key lives in this file. It is meant to be public: the
  real protection is Row Level Security on the table (INSERT-only for `anon`,
  see feedback_setup.sql). NEVER put the `service_role` key here.
* Supabase free projects get paused after ~a week of inactivity. So a failed
  send must never show the user an error: the feedback is saved to a small JSON
  file (`.state/pending_feedback.json`) and re-sent automatically the next time
  the app starts (or after any later successful send).
* Each record carries a client-generated UUID as its primary key, so a retry
  after a lost response can't create duplicates (the server answers 409, which
  we treat as "already sent").
"""

import json
import os
import platform
import queue
import socket
import threading
import tkinter as tk
import urllib.error
import urllib.request
import uuid
from tkinter import messagebox, ttk

from state_manager import STATE_FOLDER

# ----------------------------------------------------------------------------
# Configuration - fill these two in with your project's values
# (Supabase dashboard -> Project Settings -> API). Use the ANON / public key.
# ----------------------------------------------------------------------------
SUPABASE_URL = "https://ujewlcdxibasqnzjlcfi.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InVqZXdsY2R4aWJhc3FuempsY2ZpIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTAxOTk0NTUsImV4cCI6MjEwNTc3NTQ1NX0.0viXXhZz0z7aNzez8ViqWcFUFoeZW5lFp4KEctCW_og"
FEEDBACK_TABLE = "feedback"

# Set to True while testing: the "saved for later" message then also shows the
# technical reason the send failed (e.g. HTTP 401 = wrong key). Keep False in
# releases so normal users never see technical details.
DEBUG_SHOW_ERRORS = False

REQUEST_TIMEOUT = 8  # seconds - the UI never waits longer than this
MAX_PENDING = 20     # max unsent feedbacks kept on disk

# Must match the CHECK constraints in feedback_setup.sql
LIMITS = {"name": 60, "text": 2000, "log": 20000, "app_version": 20, "os": 100}


# ============================================================================
# Networking + offline queue (no Tk in this part)
# ============================================================================
def is_configured() -> bool:
    return (
        SUPABASE_URL.startswith("https://")
        and "YOUR-" not in SUPABASE_URL
        and "YOUR-" not in SUPABASE_ANON_KEY
    )


last_error = ""  # why the most recent send failed (for DEBUG_SHOW_ERRORS)


def _post(record: dict) -> str:
    """Returns "sent", "retry" (server unreachable/paused - try later) or
    "rejected" (server refused the data itself - retrying is pointless)."""
    global last_error
    if not is_configured():
        last_error = "SUPABASE_URL / SUPABASE_ANON_KEY are not filled in"
        return "retry"
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{FEEDBACK_TABLE}"
    req = urllib.request.Request(
        url,
        data=json.dumps(record, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "apikey": SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
            "Content-Type": "application/json; charset=utf-8",
            # INSERT-only RLS: we must not ask the server to return the row
            # (that would need SELECT permission and make the request fail).
            "Prefer": "return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return "sent" if 200 <= resp.status < 300 else "retry"
    except urllib.error.HTTPError as e:  # must come before URLError (subclass)
        try:
            last_error = f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
        except Exception:
            last_error = f"HTTP {e.code}"
        if e.code == 409:          # same id already stored -> a previous try worked
            return "sent"
        if e.code in (400, 422):   # constraint / validation failure
            return "rejected"
        return "retry"             # 401/403/404/5xx... e.g. paused project
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as e:
        last_error = f"{type(e).__name__}: {e}"
        return "retry"


_lock = threading.Lock()
_flushing = False


def _pending_path():
    STATE_FOLDER.mkdir(parents=True, exist_ok=True)
    return STATE_FOLDER / "pending_feedback.json"


def _load_pending() -> list:
    try:
        data = json.loads(_pending_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_pending(items: list) -> None:
    path = _pending_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _enqueue(record: dict) -> None:
    with _lock:
        items = _load_pending()
        items.append(record)
        _save_pending(items[-MAX_PENDING:])


def flush_pending() -> None:
    """Tries to send everything saved locally. Stops at the first "retry"
    (server still unreachable) and keeps the remaining items for next time."""
    global _flushing
    with _lock:
        if _flushing:
            return
        items = _load_pending()
        if not items or not is_configured():
            return
        _flushing = True
    try:
        done_ids = set()
        for rec in items:
            if _post(rec) == "retry":
                break
            done_ids.add(rec.get("id"))  # sent, or rejected (drop it)
        if done_ids:
            with _lock:
                current = _load_pending()  # may have grown while we were sending
                _save_pending([r for r in current if r.get("id") not in done_ids])
    finally:
        _flushing = False


def flush_pending_async() -> None:
    """Call once at app start. Never blocks and never raises."""
    def _run():
        try:
            flush_pending()
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def submit(record: dict) -> str:
    """Blocking (call from a worker thread). Returns "sent", "queued" or
    "rejected". Failures to reach the server are NOT errors: the record is
    queued on disk and sent later."""
    result = _post(record)
    if result == "sent":
        try:
            flush_pending()  # connection works -> also clear any backlog
        except Exception:
            pass
        return "sent"
    if result == "rejected":
        return "rejected"
    _enqueue(record)
    return "queued"


def build_record(name, rating, general, changes, problems, app_version, log="") -> dict:
    def clip(s, n):
        return (s or "").strip()[:n]
    return {
        "id": str(uuid.uuid4()),
        "name": clip(name, LIMITS["name"]) or None,
        "rating": int(rating),
        "general_feedback": clip(general, LIMITS["text"]),
        "requested_changes": clip(changes, LIMITS["text"]),
        "problems": clip(problems, LIMITS["text"]),
        "app_version": clip(app_version, LIMITS["app_version"]),
        "os": clip(f"{platform.system()} {platform.release()} ({platform.version()})", LIMITS["os"]),
        # keep the END of the log - the most recent events matter most
        "log": ((log or "").strip()[-LIMITS["log"]:]) or None,
    }


# ============================================================================
# UI
# ============================================================================
_open_dialog = None  # so the window can't be opened twice


def _fix_shortcuts(widget) -> None:
    """Ctrl+V/C/X/A keyed on the physical keycode, so they keep working while
    an Arabic keyboard layout is active (same idea as first_run_setup)."""
    handlers = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}

    def _on_ctrl(event):
        virtual = handlers.get(event.keycode)
        if virtual is None:
            return None
        widget.event_generate(virtual)
        return "break"

    widget.bind("<Control-KeyPress>", _on_ctrl)


def show_feedback_dialog(root, palette, app_version, get_log=None):
    """Opens the modal feedback window.

    palette     : the app's PALETTE dict (uses "feedback"/"feedback_dark" if present)
    app_version : e.g. "1.4.0" (sent with the message)
    get_log     : optional callable returning the event-log text (only sent if
                  the user ticks the checkbox)
    """
    global _open_dialog
    if _open_dialog is not None and _open_dialog.winfo_exists():
        _open_dialog.lift()
        _open_dialog.focus_force()
        return

    p = palette
    accent = p.get("feedback", "#d97706")
    accent_dark = p.get("feedback_dark", "#b45309")

    win = tk.Toplevel(root)
    _open_dialog = win
    win.title("Rate StudyNotes")
    win.configure(bg=p["bg"])
    win.resizable(False, False)
    win.transient(root)
    win.grab_set()

    WIDTH = 520

    # ---------- Header ----------
    header = tk.Frame(win, bg=accent, width=WIDTH)
    header.pack(fill="x")
    header.pack_propagate(False)
    header.configure(height=64)
    tk.Label(
        header, text="💬  Rate StudyNotes", bg=accent, fg="white",
        font=("Segoe UI", 14, "bold"), anchor="w",
    ).pack(side="left", padx=20, pady=14)

    body = tk.Frame(win, bg=p["bg"])
    body.pack(fill="both", expand=True, padx=20, pady=(16, 8))

    tk.Label(
        body,
        text="Tell us what you think, what you'd like to see added, and any "
             "problems you ran into. You can write in Arabic or English.",
        bg=p["bg"], fg=p["text_muted"], font=("Segoe UI", 9),
        justify="left", wraplength=WIDTH - 40,
    ).pack(anchor="w", pady=(0, 14))

    def make_card(strip_color, icon, title, subtitle=None, hint=None):
        """Same look as the cards in Model Settings: white card, colored strip
        on the left, icon + bold title, muted subtitle."""
        card = tk.Frame(
            body, bg=p["card"], highlightthickness=1,
            highlightbackground=p["border"], highlightcolor=p["border"],
        )
        card.pack(fill="x", pady=(0, 9))
        tk.Frame(card, bg=strip_color, width=5).pack(side="left", fill="y")
        inner = tk.Frame(card, bg=p["card"])
        inner.pack(side="left", fill="both", expand=True, padx=16, pady=10)
        row = tk.Frame(inner, bg=p["card"])
        row.pack(fill="x", anchor="w")
        tk.Label(row, text=icon, bg=p["card"], fg=strip_color,
                 font=("Segoe UI", 13)).pack(side="left")
        tk.Label(row, text=title, bg=p["card"], fg=p["text"],
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(8, 0))
        if hint:
            tk.Label(row, text=hint, bg=p["card"], fg=p["text_muted"],
                     font=("Segoe UI", 8)).pack(side="left", padx=(8, 0))
        if subtitle:
            tk.Label(inner, text=subtitle, bg=p["card"], fg=p["text_muted"],
                     font=("Segoe UI", 8), justify="left").pack(anchor="w", pady=(2, 6))
        return inner

    # ---------- Card 1: name + rating ----------
    top = make_card(p["info"], "👤", "About you", "Name is optional. Rating is required.")

    name_var = tk.StringVar()
    name_entry = ttk.Entry(top, textvariable=name_var, width=40, justify="left")
    name_entry.pack(anchor="w", fill="x", pady=(0, 10))
    _fix_shortcuts(name_entry)

    rating_row = tk.Frame(top, bg=p["card"])
    rating_row.pack(anchor="w", fill="x")
    rating = {"value": 0}
    star_labels = []
    rating_names = {0: "Tap a star", 1: "Poor", 2: "Fair", 3: "Good", 4: "Very good", 5: "Excellent"}
    rating_text = tk.Label(rating_row, text=rating_names[0], bg=p["card"],
                           fg=p["text_muted"], font=("Segoe UI", 9, "bold"))

    def paint(n):
        for i, lbl in enumerate(star_labels, start=1):
            lbl.config(text="★" if i <= n else "☆", fg="#f59e0b" if i <= n else "#c4c7d2")
        rating_text.config(text=rating_names[n])

    def set_rating(n):
        rating["value"] = n
        paint(n)

    for i in range(1, 6):
        lbl = tk.Label(rating_row, text="☆", bg=p["card"], fg="#c4c7d2",
                       font=("Segoe UI", 20), cursor="hand2")
        lbl.pack(side="left")
        lbl.bind("<Enter>", lambda e, n=i: paint(n))
        lbl.bind("<Button-1>", lambda e, n=i: set_rating(n))
        star_labels.append(lbl)
    rating_row.bind("<Leave>", lambda e: paint(rating["value"]))
    rating_text.pack(side="left", padx=(12, 0))

    # ---------- Cards 2-4: the three text boxes ----------
    text_boxes = []

    def make_text_card(strip_color, icon, title, hint):
        inner = make_card(strip_color, icon, title, hint=hint)
        # width=10 on purpose: real width comes from fill="x", otherwise the
        # default 80-char width would stretch the whole window.
        box = tk.Text(
            inner, height=3, width=10, wrap="word", font=("Segoe UI", 10),
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=p["border"], highlightcolor=accent,
            bg="#ffffff", fg=p["text"], undo=True, padx=6, pady=4,
        )
        box.pack(fill="x", pady=(6, 0))
        _fix_shortcuts(box)
        text_boxes.append(box)
        return box

    general_box = make_text_card(p["success"], "⭐", "What do you think of the app?",
                                 "impression, what you liked")
    changes_box = make_text_card(p["accent"], "🛠", "What should we add or change?",
                                 "features or improvements")
    problems_box = make_text_card(p["danger"], "🐞", "Problems or flaws you found",
                                  "bugs, confusing parts, annoyances")

    # ---------- Optional log ----------
    include_log = tk.BooleanVar(value=False)
    if get_log is not None:
        tk.Checkbutton(
            body, text="Include my event log (helps us find bugs; may contain lecture names)",
            variable=include_log, bg=p["bg"], activebackground=p["bg"],
            fg=p["text_muted"], font=("Segoe UI", 8), anchor="w",
            selectcolor="#ffffff", cursor="hand2",
        ).pack(anchor="w", pady=(0, 4))

    status = tk.Label(body, text="", bg=p["bg"], font=("Segoe UI", 9, "bold"),
                      justify="left", wraplength=WIDTH - 40, fg=p["danger"])
    status.pack(anchor="w", pady=(2, 0))

    # ---------- Footer ----------
    footer = tk.Frame(win, bg=p["bg"])
    footer.pack(fill="x", padx=20, pady=(8, 18))

    def close():
        global _open_dialog
        _open_dialog = None
        try:
            win.grab_release()
        except tk.TclError:
            pass
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", close)

    tk.Button(
        footer, text="Cancel", command=close, font=("Segoe UI", 9),
        bg=p["bg"], fg=p["text_muted"], relief="flat", bd=0,
        cursor="hand2", padx=8, pady=6,
    ).pack(side="left")

    send_btn = tk.Button(
        footer, text="Send feedback", font=("Segoe UI", 10, "bold"),
        bg=accent, fg="white", activebackground=accent_dark, activeforeground="white",
        relief="flat", bd=0, cursor="hand2", padx=14, pady=8,
    )
    send_btn.pack(side="right")
    send_btn.bind("<Enter>", lambda e: send_btn.config(bg=accent_dark) if str(send_btn["state"]) == "normal" else None)
    send_btn.bind("<Leave>", lambda e: send_btn.config(bg=accent) if str(send_btn["state"]) == "normal" else None)

    results: "queue.Queue[str]" = queue.Queue()

    def poll():
        if not win.winfo_exists():
            return
        try:
            result = results.get_nowait()
        except queue.Empty:
            win.after(100, poll)
            return
        if result == "sent":
            messagebox.showinfo(
                "Thank you! 💛",
                "Your feedback reached us.\nIt really helps us make StudyNotes better!",
                parent=win,
            )
            close()
        elif result == "queued":
            msg = ("Your feedback is safe with us.\nWe'll deliver it automatically "
                   "in the background as soon as we can - there's nothing else "
                   "you need to do.")
            if DEBUG_SHOW_ERRORS and last_error:
                msg += f"\n\n[debug] {last_error}"
            messagebox.showinfo("Thanks a lot! 💛", msg, parent=win)
            close()
        else:  # rejected - the server refused the data itself
            status.config(text="The server rejected this message. Please shorten it and try again.")
            send_btn.config(state="normal", text="Send feedback", bg=accent)

    def on_send():
        def text_of(box):
            return box.get("1.0", "end").strip()

        general, changes, problems = text_of(general_box), text_of(changes_box), text_of(problems_box)
        if rating["value"] == 0:
            status.config(text="Please choose a star rating first.")
            return
        if not (general or changes or problems):
            status.config(text="Please write something in at least one of the boxes.")
            return

        log_text = ""
        if get_log is not None and include_log.get():
            try:
                log_text = get_log() or ""
            except Exception:
                log_text = ""

        record = build_record(name_var.get(), rating["value"], general, changes,
                              problems, app_version, log_text)
        status.config(text="", fg=p["danger"])
        send_btn.config(state="disabled", text="Sending…", bg=p["text_muted"])

        def worker():
            try:
                results.put(submit(record))
            except Exception:
                results.put("queued")  # never surface an error to the user
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, poll)

    send_btn.config(command=on_send)

    # ---------- Center over the main window ----------
    win.update_idletasks()
    if win.winfo_reqheight() > win.winfo_screenheight() - 90:
        for box in text_boxes:
            box.config(height=2)
        win.update_idletasks()
    w = win.winfo_reqwidth() + 4
    h = win.winfo_reqheight() + 4
    screen_w, screen_h = win.winfo_screenwidth(), win.winfo_screenheight()
    x = root.winfo_x() + (root.winfo_width() - w) // 2
    y = root.winfo_y() + (root.winfo_height() - h) // 2
    x = max(0, min(x, screen_w - w))
    y = max(0, min(y, screen_h - h - 40))
    win.geometry(f"{w}x{h}+{x}+{y}")
    name_entry.focus_set()