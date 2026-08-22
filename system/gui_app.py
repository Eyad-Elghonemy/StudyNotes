"""
واجهة رسومية لنظام تسجيل وتفريغ وتحويل محاضرات/جلسات لنوتس.

تشغيل: python gui_app.py

--------------------------------------------------------------------------
ملاحظة عن العربي/الإنجليزي المختلط:
--------------------------------------------------------------------------
Tk على ويندوز بيستخدم محرك النصوص بتاع الويندوز نفسه (Uniscribe/DirectWrite)
لرسم أي نص في widget عادي (Button, Label, LabelFrame, Checkbutton,
messagebox...)، وده بيعمل shaping (وصل حروف العربي) وbidi reordering
(ترتيب عربي/إنجليزي/أرقام) صح تلقائيًا من غير أي تدخل يدوي. أي محاولة
لعمل reshape/bidi يدوي (زي arabic_reshaper + python-bidi) على نص هيتحط
في widget من دول بتبوظه، لأنها بتعالج نص هيتعالج تاني مرة من جوه Tk
نفسه، فبيطلع مقلوب بالكامل.

الاستثناء الوحيد اللي فعلاً محتاج معالجة يدوية هو الـ Text widget (سجل
الأحداث تحت) لأنه بيتعامل مع النص كـ"سطر خام" بترتيب الكتابة (insertion
order) من غير تفسير bidi للفقرة ككل، فبيخلط ترتيب "[timestamp] رسالة"
لو كان فيها عربي وإنجليزي مع بعض - فده اللي فضل يستخدم python-bidi هنا
بس، زي ما كان في النسخة الأصلية.
"""

import os
import re
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, scrolledtext, simpledialog, ttk

import numpy as np
import soundcard as sc
import soundfile as sf
from dotenv import load_dotenv

import arabic_reshaper
from bidi.algorithm import get_display
import markdown as md_lib
from tkinterweb import HtmlFrame

from math_render import render_math_to_html_images

load_dotenv()

from state_manager import (
    RECORD_FOLDER,
    TRANSCRIPT_FOLDER,
    MARKDOWN_FOLDER,
    list_existing_lectures,
    list_lecture_chunks,
    pending_summary,
    load_state,
    save_state,
    safe_name,
    compress_to_opus,
    ffmpeg_available,
    delete_lecture_data,
    delete_specific_files,
    check_disk_space_mb,
    LOW_DISK_WARNING_MB,
    audio_duration_minutes_safe,
)
import process_lecture

APP_VERSION = "1.1.0"

SAMPLE_RATE = 16000
CHUNK_MINUTES = 30
LONG_RECORDING_REMINDER_MINUTES = 120  # تنبيه (مش إيقاف) كل ساعتين تسجيل مستمر

STATUS_LABELS = {
    "recorded": "متسجل بس",
    "transcribed": "متفرّغ",
    "explained": "متفرّغ ومتلخص",
}
STATUS_COLORS = {
    "recorded": "#8a8f98",
    "transcribed": "#b8860b",
    "explained": "#1f8a4c",
}

# لوحة الألوان الموحّدة لكل التصميم - عشان الشكل يبقى متناسق بدل ما كل
# زرار يكون بستايل مستقل عن التاني.
PALETTE = {
    "bg": "#f4f5f9",
    "card": "#ffffff",
    "border": "#e2e4ec",
    "text": "#22242c",
    "text_muted": "#6b6f7d",
    "accent": "#4c4ddc",
    "accent_dark": "#3d3eb0",
    "danger": "#c62828",
    "danger_dark": "#a92121",
    "success": "#1f8a4c",
    "success_dark": "#186e3c",
    "info": "#0f6fb0",
    "info_dark": "#0c5a8f",
    "warning": "#c9950c",
    "warning_bg": "#fff3e0",
    "neutral_bg": "#666666",
    # درجات أهدأ (Tier 2/3) - مستخدمة عشان تدرّج الأهمية البصرية بين
    # الزرار الرئيسي (الأقوى/الأشبع) والزراير الأقل أهمية تحته، بدل ما
    # كل الزراير تبان بنفس قوة اللون فتلخبط العين إيه الأهم فعلاً.
    "success_soft": "#4faa78",
    "success_soft_dark": "#3d8c62",
    "info_soft": "#4a90c4",
    "info_soft_dark": "#3a76a3",
    "accent_soft": "#7677e0",
    "accent_soft_dark": "#5f60c4",
}

# ألوان صناديق التمييز (blockquotes) في عارض النوتس - كل نوع من الصناديق
# اللي البرومبت بتاع Gemini بيولّدها (راجع EXPLAIN_PROMPT في process_lecture.py)
# ليه لون مختلف عشان يبقى واضح بصرياً من أول نظرة وقت المراجعة.
HIGHLIGHT_STYLES = {
    "💡": ("hl-important", "#fff8e1", "#d4a017"),   # مهم/تأكيد
    "🎯": ("hl-interview", "#eaf1ff", "#2f6fdb"),   # سؤال إنترفيو محتمل
    "⚠️": ("hl-warning", "#fdeaea", "#c0392b"),     # تنبيه/تحذير
    "✅": ("hl-task", "#e8f8ee", "#1f8a4c"),         # تاسك/مطلوب تنفيذه
    "📚": ("hl-resource", "#f1eafc", "#7b4fd1"),     # مصدر إضافي
    "📌": ("hl-definition", "#eaf6f8", "#1690a0"),   # تعريف
    "🔁": ("hl-summary", "#f2f2f2", "#6b6f7d"),      # خلاصة
    "🧩": ("hl-example", "#fff1e6", "#d4772c"),      # مثال محلول
    "🕒": ("hl-deadline", "#fdecec", "#b03a2e"),     # ميعاد/امتحان
}


def _colorize_blockquotes(html_body: str) -> str:
    """
    بتدوّر على كل <blockquote> في HTML الناتج من الماركداون، وتشوف بيبدأ
    بأنهي إيموجي من HIGHLIGHT_STYLES، وتضيفله class مناسب عشان الـ CSS
    يلوّنه بلونه الخاص بدل ما كل الصناديق تبقى بنفس اللون الأصفر الموحّد.
    """
    def repl(match: "re.Match") -> str:
        inner = match.group(1)
        stripped = re.sub(r"^\s*<p>", "", inner).lstrip()
        for emoji, (css_class, _bg, _border) in HIGHLIGHT_STYLES.items():
            if stripped.startswith(emoji):
                return f'<blockquote class="{css_class}">{inner}</blockquote>'
        return match.group(0)

    return re.sub(r"<blockquote>(.*?)</blockquote>", repl, html_body, flags=re.DOTALL)


class _Tooltip:
    """Tooltip بسيط بيظهر لما الماوس يوقف على أي widget."""

    def __init__(self, widget, text: str, delay_ms: int = 500):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id = None
        self._tip_win = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self):
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self):
        if self._tip_win is not None:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip_win = tk.Toplevel(self.widget)
        self._tip_win.wm_overrideredirect(True)
        self._tip_win.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            self._tip_win, text=self.text, justify="right",
            background="#2b2d38", foreground="#ffffff",
            relief="solid", borderwidth=0,
            font=("Segoe UI", 9), padx=8, pady=4,
        )
        label.pack()

    def _hide(self, _event=None):
        self._cancel()
        if self._tip_win is not None:
            self._tip_win.destroy()
            self._tip_win = None


def _add_tooltip(widget, text: str):
    _Tooltip(widget, text)


def _beep():
    """تنبيه صوتي بسيط، وميهمش لو فشل (مثلاً على نظام مش بيدعمه)."""
    try:
        import winsound
        winsound.MessageBeep()
    except Exception:
        try:
            print("\a", end="", flush=True)
        except Exception:
            pass


def show_info(title, message):
    return messagebox.showinfo(title, message)


def show_warning(title, message):
    return messagebox.showwarning(title, message)


def show_error(title, message):
    return messagebox.showerror(title, message)


def ask_yesno(title, message):
    return messagebox.askyesno(title, message)


def _fmt_min_mb(minutes: float, mb: float) -> str:
    """نص المدة/الحجم بالإنجليزي بالكامل ("6.2 min | 0.1 MB") بدل خلط
    كلمة عربي ("دقيقة") مع أرقام و"MB" في نفس اللابل - الخلط ده هو اللي
    كان بيخلي Tk يلخبط ترتيب العرض جوه سياق RTL. نص إنجليزي بالكامل زي
    ده بيتعرض بترتيبه الصح دايمًا بغض النظر عن اتجاه اللي حواليه."""
    return f"{minutes:.1f} min | {mb:.1f} MB"


# =========================================================================
# عزل الاتجاه (Unicode Bidi Isolates) لسجل الأحداث
# -------------------------------------------------------------------------
# سطر اللوج بيخلط عربي + إنجليزي (أسماء أجهزة) + أرقام/وقت + أقواس متداخلة
# + أسهم/إيموجي. تشغيل السطر كله دفعة واحدة على get_display() (اللي بتنفذ
# UAX#9) بيتلخبط في الحالتين دول بالذات: (1) أقواس متداخلة زي
# "(Realtek(R) Audio)" واللي بتاعتها mirroring حسب الـ embedding level،
# و(2) رموز محايدة زي → و⚠️ بتاخد اتجاه غير متوقع من الحرف اللي جنبها.
#
# الحل: بدل ما نسيب get_display() تحاول "تخمّن" ترتيب السطر المعقد كله،
# بنحوّط كل جزء لاتيني/رقمي/رمزي بعلامات عزل LRI...PDI عشان يتعامل معاه
# كجزيرة متماسكة منفصلة عن سياق العربي المحيط بيه - وده حل عام بيغطي أي
# رسالة جديدة تتضاف بعدين، مش باتش لكل رسالة لوحدها.
# =========================================================================
LRI = "\u2066"  # Left-to-Right Isolate
PDI = "\u2069"  # Pop Directional Isolate

_NON_ARABIC_RUN_RE = re.compile(
    r"[A-Za-z0-9\(\)\[\]\-_./:%→←↔⚠✓✗🎧🔴⏱⏳✅💡🗑↩📌📋]+"
    r"(?:[ \t]+[A-Za-z0-9\(\)\[\]\-_./:%→←↔]+)*"
)


def _isolate_ltr_runs(text: str) -> str:
    """يحوّط أي جزء لاتيني/رقمي/رمزي داخل السطر بعلامات LRI...PDI عشان
    خوارزمية bidi تعامله كوحدة منفصلة، بدل ما تدمجه جوه سياق العربي وتكسر
    ترتيب الأقواس/الأسهم اللي جواه."""
    return _NON_ARABIC_RUN_RE.sub(lambda m: f"{LRI}{m.group(0)}{PDI}", text)


def _get_default_app_name(ext: str = ".md") -> str:
    """
    بيجيب اسم البرنامج الافتراضي المربوط بامتداد الملف ده على ويندوز
    (فعلياً، مش افتراض)، عشان زرار "افتح الملف" يوضح صح هيفتح بإيه.
    بيرجع اسم عام ("البرنامج الافتراضي") لو التعرف فشل أو مش على ويندوز.
    """
    try:
        import ctypes

        ASSOCF_NONE = 0
        ASSOCSTR_FRIENDLYAPPNAME = 6

        shlwapi = ctypes.windll.shlwapi
        buf_len = ctypes.c_uint(0)
        shlwapi.AssocQueryStringW(
            ASSOCF_NONE, ASSOCSTR_FRIENDLYAPPNAME, ext, None, None, ctypes.byref(buf_len)
        )
        if buf_len.value == 0:
            return "البرنامج الافتراضي"

        buf = ctypes.create_unicode_buffer(buf_len.value)
        result = shlwapi.AssocQueryStringW(
            ASSOCF_NONE, ASSOCSTR_FRIENDLYAPPNAME, ext, None, buf, ctypes.byref(buf_len)
        )
        if result != 0 or not buf.value:
            return "البرنامج الافتراضي"
        return buf.value
    except Exception:
        return "البرنامج الافتراضي"


class StudyApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"مسجّل ومفرّغ محاضرات وجلسات — v{APP_VERSION}")
        self.root.geometry("980x800")
        self.root.minsize(860, 660)
        # لو الشاشة نفسها أقصر من الحجم المطلوب، نقلّل ارتفاع النافذة
        # عشان "سجل الأحداث" ميتقفلش برّه حدود الشاشة تحت التاسك بار.
        screen_h = self.root.winfo_screenheight()
        if screen_h - 90 < 800:
            self.root.geometry(f"980x{max(660, screen_h - 90)}")
        self.root.configure(bg=PALETTE["bg"])

        self.audio_queue: "queue.Queue[np.ndarray]" = queue.Queue()
        self.stop_flag = threading.Event()
        self.recording = False
        self.current_lecture = None
        self._write_thread = None
        self.chunk_vars = {}

        self._recording_start_time = None
        self._pause_started_at = None  # وقت بدء الاستراحة الحالية، لو فيه
        self._elapsed_timer_job = None
        self._last_long_reminder_minutes = 0
        self._log_plain_lines: list[str] = []

        self.paused = False
        self._processing = False  # في تفريغ/تلخيص شغال دلوقتي (لتحذير الإغلاق)
        self._cancel_processing_event = threading.Event()
        process_lecture.set_cancel_event(self._cancel_processing_event)

        self._setup_style()
        self._build_ui()
        self._refresh_lecture_list()
        self._bind_shortcuts()

        process_lecture.set_logger(self._log_threadsafe)
        process_lecture.set_progress_callback(self._progress_threadsafe)

    # ---------------------------------------------------------- Style
    def _setup_style(self):
        """ثيم ttk موحّد لكل الفريمات/الكومبوبوكس/الـ checkbuttons، عشان
        الشكل يبقى متناسق بدل خليط ثيمات وألوان افتراضية مختلفة."""
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            try:
                style.theme_use("clam")
            except Exception:
                pass

        style.configure("TFrame", background=PALETTE["bg"])
        style.configure(
            "Card.TLabelframe", background=PALETTE["card"],
        )
        style.configure(
            "Card.TLabelframe.Label", background=PALETTE["card"],
            foreground=PALETTE["text"], font=("Segoe UI", 10, "bold"),
        )
        style.configure("Card.TFrame", background=PALETTE["card"])
        style.configure(
            "Muted.TLabel", background=PALETTE["card"],
            foreground=PALETTE["text_muted"], font=("Segoe UI", 9),
        )
        style.configure(
            "Header.TLabel", background=PALETTE["bg"],
            foreground=PALETTE["text"], font=("Segoe UI", 15, "bold"),
        )
        style.configure(
            "Badge.TLabel", background=PALETTE["bg"],
            foreground=PALETTE["warning"], font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "Total.TLabel", background=PALETTE["card"],
            foreground=PALETTE["accent_dark"], font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "Toolbar.TButton", font=("Segoe UI", 9), padding=(10, 5),
        )
        # شريط تقدّم بحجم/لون واضح ومحدود العرض، بدل الستايل الافتراضي
        # المسطح اللي كان بيبان كخط رفيع ممتد على عرض الكارت كله.
        style.configure(
            "App.Horizontal.TProgressbar",
            troughcolor=PALETTE["border"], background=PALETTE["accent"],
            bordercolor=PALETTE["border"], lightcolor=PALETTE["accent"],
            darkcolor=PALETTE["accent"], thickness=10,
        )

    def _card_button(self, parent, text, command, bg, bg_hover, fg="white",
                      font=("Segoe UI", 10, "bold"), **kw):
        """زرار مصبوغ موحّد الشكل (padding/relief/hover) - عشان كل زراير
        الألوان في البرنامج تبقى بنفس الطراز بدل ستايلات متفرقة."""
        btn = tk.Button(
            parent, text=text, command=command,
            font=font, bg=bg, fg=fg,
            activebackground=bg_hover, activeforeground=fg,
            cursor="hand2",
            highlightthickness=0,
            relief=kw.pop("relief", "flat"),
            bd=kw.pop("bd", 0),
            padx=kw.pop("padx", 14), pady=kw.pop("pady", 8),
            **kw,
        )
        btn._normal_bg = bg
        btn.bind("<Enter>", lambda e: btn.config(bg=bg_hover))
        btn.bind("<Leave>", lambda e: btn.config(bg=btn._normal_bg))
        return btn

    def _outline_button(self, parent, text, command, color, **kw):
        btn = tk.Button(
            parent, text=text, command=command,
            font=("Segoe UI", 9, "bold"), bg="#ffffff", fg=color,
            activebackground="#f0f1ff", activeforeground=color,
            relief="solid", bd=1, highlightbackground=color,
            cursor="hand2", padx=kw.pop("padx", 12), pady=kw.pop("pady", 6),
            **kw,
        )
        btn.bind("<Enter>", lambda e: btn.config(bg="#f0f1ff"))
        btn.bind("<Leave>", lambda e: btn.config(bg="#ffffff"))
        return btn

    # ---------------------------------------------------------- Shortcuts
    def _bind_shortcuts(self):
        """
        Space / Ctrl+R لبدء أو إيقاف التسجيل من غير الماوس. مربوطة على
        مستوى الـ root، لكن بنتجاهلها لو الفوكس في حقل كتابة (زي اسم
        محاضرة جديدة) عشان مايبقاش الضغط على مسافة أثناء الكتابة يوقف
        التسجيل بالغلط.
        """

        def handle_space(event):
            widget = event.widget
            if isinstance(widget, (tk.Entry, tk.Text)):
                return  # سيبها تكتب مسافة عادي
            self._toggle_recording()
            return "break"

        self.root.bind("<space>", handle_space)
        self.root.bind("<Control-r>", lambda e: self._toggle_recording())
        self.root.bind("<Control-R>", lambda e: self._toggle_recording())

    # ---------------------------------------------------------- UI Layout
    def _build_ui(self):
        pad = {"padx": 12, "pady": 5}

        # ---------- المحاضرة/الجلسة ----------
        # (شلنا صف هيدر منفصل بعنوان البرنامج عشان نوفّر مساحة رأسية -
        # اسم البرنامج أصلاً ظاهر في شريط عنوان النافذة فوق)
        frame_top = ttk.LabelFrame(self.root, text="🎓 المحاضرة / الجلسة", style="Card.TLabelframe")
        frame_top.pack(fill="x", padx=12, pady=(10, 5))

        self.lecture_var = tk.StringVar()
        self.lecture_combo = ttk.Combobox(
            frame_top, textvariable=self.lecture_var, state="readonly", width=42,
            justify="right",
        )
        self.lecture_combo.pack(side="right", padx=10, pady=10)
        self.lecture_combo.bind("<<ComboboxSelected>>", lambda e: self._on_lecture_change())

        new_lecture_btn = self._outline_button(
            frame_top, "➕ جديدة", self._new_lecture_dialog, PALETTE["accent"],
        )
        new_lecture_btn.pack(side="right", padx=10)
        _add_tooltip(new_lecture_btn, "إنشاء محاضرة/جلسة جديدة بالاسم")

        self.pending_badge = ttk.Label(frame_top, text="", style="Badge.TLabel", background=PALETTE["card"])
        self.pending_badge.pack(side="right", padx=12)

        # ---------- التحكم في التسجيل ----------
        frame_controls = ttk.LabelFrame(self.root, text="⏺ التسجيل", style="Card.TLabelframe")
        frame_controls.pack(fill="x", **pad)
        controls_row = ttk.Frame(frame_controls, style="Card.TFrame")
        controls_row.pack(fill="x", padx=10, pady=6)

        self.record_btn = self._card_button(
            controls_row, "▶️  ابدأ التسجيل", self._toggle_recording,
            PALETTE["danger"], PALETTE["danger_dark"],
        )
        self.record_btn.pack(side="right")
        _add_tooltip(self.record_btn, "بدء/إيقاف التسجيل (اختصار: Ctrl+R أو Space)")

        self.pause_btn = self._outline_button(
            controls_row, "⏸ إيقاف مؤقت", self._toggle_pause, PALETTE["warning"],
        )
        # بيبان بس وقت التسجيل الفعلي - مفيش معنى لإيقاف مؤقت وإحنا واقفين أصلاً
        _add_tooltip(self.pause_btn, "وقف التقاط الصوت مؤقتاً (زي استراحة) من غير ما تقفل الجزء الحالي")

        self.status_label = ttk.Label(
            controls_row, text="✅ جاهز", foreground=PALETTE["text_muted"],
            background=PALETTE["card"], font=("Segoe UI", 10),
        )
        self.status_label.pack(side="right", padx=12)

        self.timer_label = tk.Label(
            controls_row, text="", fg=PALETTE["danger"], bg=PALETTE["card"],
            font=("Consolas", 13, "bold"),
        )
        self.timer_label.pack(side="right", padx=8)

        self.delete_after_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            controls_row, text="🧹 امسح الصوت بعد التفريغ الناجح", variable=self.delete_after_var,
        ).pack(side="left", padx=8)

        # ---------- أجزاء التسجيل ----------
        frame_chunks = ttk.LabelFrame(self.root, text="🎧 أجزاء التسجيل", style="Card.TLabelframe")
        frame_chunks.pack(fill="both", expand=True, **pad)

        chunk_toolbar = ttk.Frame(frame_chunks, style="Card.TFrame")
        chunk_toolbar.pack(fill="x", padx=8, pady=6)

        select_all_btn = ttk.Button(
            chunk_toolbar, text="✅ تحديد الكل", command=self._select_all_chunks,
            style="Toolbar.TButton",
        )
        select_all_btn.pack(side="right", padx=3)
        _add_tooltip(select_all_btn, "تحديد كل الأجزاء المعروضة")

        deselect_btn = ttk.Button(
            chunk_toolbar, text="⬜ إلغاء التحديد", command=self._deselect_all_chunks,
            style="Toolbar.TButton",
        )
        deselect_btn.pack(side="right", padx=3)
        _add_tooltip(deselect_btn, "إلغاء تحديد كل الأجزاء")

        refresh_btn = ttk.Button(
            chunk_toolbar, text="🔄 تحديث القائمة", command=self._refresh_chunks,
            style="Toolbar.TButton",
        )
        refresh_btn.pack(side="right", padx=3)
        _add_tooltip(refresh_btn, "إعادة قراءة الأجزاء الموجودة على الديسك")

        # إجمالي الأجزاء المحددة دلوقتي (بيتحدّث لايف مع كل تحديد/إلغاء)
        self.selection_total_label = ttk.Label(
            chunk_toolbar, text="", style="Total.TLabel",
        )
        self.selection_total_label.pack(side="left", padx=6)

        chunks_container = ttk.Frame(frame_chunks, style="Card.TFrame")
        chunks_container.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        self._chunks_canvas = tk.Canvas(
            chunks_container, height=130, highlightthickness=0, bg=PALETTE["card"],
        )
        chunks_vsb = ttk.Scrollbar(chunks_container, orient="vertical", command=self._chunks_canvas.yview)
        self._chunks_inner = ttk.Frame(self._chunks_canvas, style="Card.TFrame")
        self._chunks_inner.bind(
            "<Configure>", lambda e: self._chunks_canvas.configure(scrollregion=self._chunks_canvas.bbox("all"))
        )
        self._chunks_canvas.create_window((0, 0), window=self._chunks_inner, anchor="nw")
        self._chunks_canvas.configure(yscrollcommand=chunks_vsb.set)
        self._chunks_canvas.pack(side="left", fill="both", expand=True)
        chunks_vsb.pack(side="right", fill="y")

        # ---------- أزرار المعالجة ----------
        frame_process = ttk.LabelFrame(self.root, text="📝 التفريغ والنوتس", style="Card.TLabelframe")
        frame_process.pack(fill="x", **pad)

        # الزرار الأهم (فرّغ + حوّل لنوتس) في صف لوحده، بارز بشكل واضح
        # عن باقي الزراير - هو اللي هيتستخدم في الأغلبية الساحقة من المرات.
        row_primary = ttk.Frame(frame_process, style="Card.TFrame")
        row_primary.pack(fill="x", padx=10, pady=(6, 3))

        self.btn_primary = self._card_button(
            row_primary, "🔄  فرّغ + حوّل المحدد لنوتس (الكل مع بعض)",
            lambda: self._start_processing(True),
            PALETTE["success"], PALETTE["success_dark"],
            font=("Segoe UI", 11, "bold"),
        )
        self.btn_primary.pack(fill="x")
        _add_tooltip(self.btn_primary, "الخطوة الأساسية: تفريغ الأجزاء المحددة، وتحويلها مباشرة لنوتس Markdown كعملية واحدة")

        # صف ثانوي: أفعال معالجة بديلة أقل استخداماً (تفريغ لوحده / نوتس
        # من نص موجود بالفعل)
        row_secondary = ttk.Frame(frame_process, style="Card.TFrame")
        row_secondary.pack(fill="x", padx=10, pady=4)
        row_secondary.columnconfigure(0, weight=1)
        row_secondary.columnconfigure(1, weight=1)

        # زراير تانوية (Tier 2 بصرياً): أصغر شوية من الرئيسي وبشكل مختلف
        # (حواف بارزة/ridge بدل مسطح تماماً) عشان تبان أوضح إنها أقل أهمية
        # من الزرار الرئيسي فوقها، وألوان أهدأ (soft) بدل الألوان الكاملة.
        btn_transcribe = self._card_button(
            row_secondary, "✍  فرّغ فقط (بدون نوتس)",
            lambda: self._start_processing(False),
            PALETTE["info_soft"], PALETTE["info_soft_dark"],
            font=("Segoe UI", 9, "bold"),
            relief="ridge", bd=2, padx=10, pady=6,
        )
        btn_transcribe.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        _add_tooltip(btn_transcribe, "تفريغ الأجزاء المحددة لنص خام فقط، من غير تحويل لنوتس")

        btn_notes_only = self._card_button(
            row_secondary, "🧠  حوّل لنوتس بس (بدون تفريغ)",
            self._start_notes_only,
            PALETTE["success_soft"], PALETTE["success_soft_dark"],
            font=("Segoe UI", 9, "bold"),
            relief="ridge", bd=2, padx=10, pady=6,
        )
        btn_notes_only.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        _add_tooltip(btn_notes_only, "تحويل النص المفرّغ الموجود بالفعل لنوتس، من غير تفريغ صوت جديد")

        # صف عرض النتيجة (Tier 3): أهم من زراير المسح/التراجع تحته، بس
        # أقل من زراير المعالجة فوقه - ألوان أهدأ برضه (soft) للتفرقة.
        default_app_name = _get_default_app_name(".md")

        row_view_notes = ttk.Frame(frame_process, style="Card.TFrame")
        row_view_notes.pack(fill="x", padx=10, pady=(2, 4))
        row_view_notes.columnconfigure(0, weight=1)
        row_view_notes.columnconfigure(1, weight=1)

        btn_view_notes = self._card_button(
            row_view_notes, "📄  عرض النوتس هنا", self._show_notes_viewer,
            PALETTE["accent_soft"], PALETTE["accent_soft_dark"],
        )
        btn_view_notes.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        _add_tooltip(btn_view_notes, "فتح نافذة معاينة للنوتس (مع المعادلات) جوه البرنامج نفسه")

        btn_open_md = self._card_button(
            row_view_notes, f"📂  افتح في {default_app_name}", self._open_markdown_file,
            PALETTE["info_soft"], PALETTE["info_soft_dark"],
        )
        btn_open_md.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        _add_tooltip(btn_open_md, f"فتح ملف الـ Markdown بالبرنامج الافتراضي على جهازك ({default_app_name})")

        # صف المسح والتراجع: زرار المسح الانتقائي على الشمال، وزرار
        # التراجع عن آخر تحديث اتنقل لليمين.
        row_undo = ttk.Frame(frame_process, style="Card.TFrame")
        row_undo.pack(fill="x", padx=10, pady=(2, 2))

        btn_delete = tk.Button(
            row_undo,
            text="🗑 مسح بيانات المحاضرة",
            command=self._open_delete_dialog,
            font=("Segoe UI", 9, "bold"),
            bg="#fdecea", fg=PALETTE["danger"],
            activebackground="#f8d7da", activeforeground=PALETTE["danger"],
            relief="solid", bd=1, highlightbackground=PALETTE["danger"],
            cursor="hand2", padx=12, pady=6,
        )
        btn_delete.pack(side="left")
        btn_delete.bind("<Enter>", lambda e: btn_delete.config(bg="#f8d7da"))
        btn_delete.bind("<Leave>", lambda e: btn_delete.config(bg="#fdecea"))
        _add_tooltip(btn_delete, "مسح انتقائي لملفات المحاضرة (صوت التسجيل/التفريغ/النوتس) - المسح نهائي")

        btn_undo = tk.Button(
            row_undo,
            text="↩ تراجع عن آخر تحديث",
            command=self._undo_last_update,
            font=("Segoe UI", 9, "bold"),
            bg=PALETTE["warning_bg"], fg="#a15c00",
            activebackground="#ffe4b5", activeforeground="#a15c00",
            relief="solid", bd=1, highlightbackground=PALETTE["warning"],
            cursor="hand2", padx=12, pady=6,
        )
        btn_undo.pack(side="right")
        btn_undo.bind("<Enter>", lambda e: btn_undo.config(bg="#ffe4b5"))
        btn_undo.bind("<Leave>", lambda e: btn_undo.config(bg=PALETTE["warning_bg"]))
        _add_tooltip(btn_undo, "تراجع عن آخر تحديث: إلغاء آخر قسم نوتس اتضاف، ورجوع الأجزاء المرتبطة به لحالة \"متفرّغ\"")

        row_progress = ttk.Frame(frame_process, style="Card.TFrame")
        row_progress.pack(fill="x", padx=10, pady=(2, 6))

        # عرض ثابت ومعقول بدل ما يمتد على عرض الكارت/الشاشة كله
        self.progress_label = ttk.Label(row_progress, text="", style="Muted.TLabel")
        self.progress_label.pack(side="right", padx=(8, 0))
        self.progress_bar = ttk.Progressbar(
            row_progress, mode="determinate",
            style="App.Horizontal.TProgressbar", length=260,
        )
        self.progress_bar.pack(side="right")

        self.btn_cancel_processing = self._outline_button(
            row_progress, "🛑 إلغاء", self._cancel_processing, PALETTE["danger"],
            padx=8, pady=3,
        )
        # مخفي طول ما مفيش عملية شغالة - بيظهر بس وقت التفريغ/التلخيص
        _add_tooltip(self.btn_cancel_processing, "وقف التفريغ/التلخيص الحالي بعد ما يخلص الجزء اللي شغال دلوقتي")

        # ---------- سجل الأحداث ----------
        frame_log = ttk.LabelFrame(self.root, text="📋 سجل الأحداث", style="Card.TLabelframe")
        frame_log.pack(fill="both", expand=True, **pad)

        row_log_toolbar = ttk.Frame(frame_log, style="Card.TFrame")
        row_log_toolbar.pack(fill="x", padx=8, pady=(6, 0))
        btn_copy_log = self._outline_button(
            row_log_toolbar, "📋 نسخ السجل", self._copy_log, PALETTE["text_muted"],
            padx=8, pady=3,
        )
        btn_copy_log.pack(side="left")
        _add_tooltip(btn_copy_log, "نسخ سجل الأحداث كامل (كنص سليم) للـ clipboard")

        self.log_box = scrolledtext.ScrolledText(
            frame_log, height=6, state="disabled", wrap="word",
            font=("Segoe UI", 10), bg=PALETTE["card"], fg=PALETTE["text"],
            relief="flat", padx=8, pady=6,
        )
        self.log_box.tag_configure("rtl_line", justify="right")
        self.log_box.pack(fill="both", expand=True, padx=8, pady=8)

        frame_paths = ttk.Frame(self.root, style="TFrame")
        frame_paths.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Label(
            frame_paths,
            text=f"الصوت: {RECORD_FOLDER}   |   النص: {TRANSCRIPT_FOLDER}   |   الملخص: {MARKDOWN_FOLDER}",
            foreground=PALETTE["text_muted"], background=PALETTE["bg"],
            font=("Segoe UI", 8),
        ).pack(side="right")
        ttk.Label(
            frame_paths, text=f"v{APP_VERSION}",
            foreground=PALETTE["text_muted"], background=PALETTE["bg"],
            font=("Segoe UI", 8),
        ).pack(side="left")

    # ---------------------------------------------------------- BiDi log
    # الـ Text widget (على عكس Button/Label العادي) مش بيفسّر اتجاه
    # الفقرة (bidi) لوحده، فلو سطر فيه عربي وإنجليزي مع بعض (زي
    # "[HH:MM:SS] رسالة عربي...") بيتلخبط ترتيبه. عشان كده استخدام
    # python-bidi هنا بالذات (وبس هنا) هو الصح.
    @staticmethod
    def _bidi_display(text: str) -> str:
        try:
            isolated = _isolate_ltr_runs(text)
            return get_display(arabic_reshaper.reshape(isolated))
        except Exception:
            return text

    def _log(self, msg: str):
        self.log_box.configure(state="normal")
        timestamp = datetime.now().strftime("%H:%M:%S")
        raw_line = f"[{timestamp}] {msg}"
        # بنحتفظ بالنسخة الخام (قبل reshape/bidi) في قايمة منفصلة عشان
        # النسخ للـ clipboard يطلع نص سليم قابل للصق في أي مكان تاني -
        # النسخة المعروضة في الـ widget معاد ترتيبها بصريًا للعرض بس.
        self._log_plain_lines.append(raw_line)
        line = self._bidi_display(raw_line)
        self.log_box.insert("end", line + "\n", "rtl_line")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _log_threadsafe(self, msg: str):
        self.root.after(0, self._log, msg)

    def _copy_log(self):
        """بينسخ سجل الأحداث كامل (النسخة الخام قبل معالجة bidi، مش
        النسخة المعروضة اللي اتعاد ترتيبها بصريًا للعرض) للـ clipboard."""
        if not self._log_plain_lines:
            show_info("تنبيه", "سجل الأحداث فاضي، مفيش حاجة تتنسخ.")
            return
        content = "\n".join(self._log_plain_lines)
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.root.update()  # عشان الـ clipboard يتحدث فعليًا حتى لو البرنامج اتقفل بعدها بسرعة
        self._log("📋 اتنسخ سجل الأحداث كامل.")

    def _progress_threadsafe(self, done: int, total: int, label: str = ""):
        def update():
            if total > 0:
                self.progress_bar["maximum"] = total
                self.progress_bar["value"] = done
                self.progress_label.config(text=f"{label} {done}/{total}")
            else:
                self.progress_bar["value"] = 0
                self.progress_label.config(text="")
        self.root.after(0, update)

    # ---------------------------------------------------------- Lectures
    def _refresh_lecture_list(self):
        lectures = list_existing_lectures()
        self.lecture_combo["values"] = lectures
        if lectures and not self.lecture_var.get():
            self.lecture_combo.current(0)
        self.current_lecture = self.lecture_var.get() or None
        self._refresh_chunks()

    def _on_lecture_change(self):
        self.current_lecture = self.lecture_var.get()
        self._refresh_chunks()

    def _new_lecture_dialog(self):
        name = simpledialog.askstring("محاضرة/جلسة جديدة", "اسم المحاضرة أو الجلسة:")
        if name:
            clean = safe_name(name)
            values = list(self.lecture_combo["values"])
            if clean not in values:
                self.lecture_combo["values"] = values + [clean]
            self.lecture_combo.set(clean)
            self._on_lecture_change()

    # ---------------------------------------------------------- Chunks panel
    def _refresh_chunks(self):
        for child in self._chunks_inner.winfo_children():
            child.destroy()
        self.chunk_vars.clear()

        lecture = self.current_lecture
        if not lecture:
            self._update_selection_total()
            return

        state = load_state(lecture)
        chunks = list_lecture_chunks(lecture, state)

        if not chunks:
            ttk.Label(
                self._chunks_inner, text="مفيش أجزاء صوت لسه لهذه المحاضرة.",
                style="Muted.TLabel",
            ).pack(anchor="e", padx=6, pady=6)
        else:
            for c in chunks:
                is_corrupted = c["corrupted"]
                # الملفات التالفة متتحددش أوتوماتيك حتى لو "متسجل بس" -
                # عشان اليوزر ميبعتهاش للتفريغ بالغلط من غير ما ياخد باله
                var = tk.BooleanVar(value=(c["status"] == "recorded" and not is_corrupted))
                var.trace_add("write", lambda *_: self._update_selection_total())
                self.chunk_vars[c["filename"]] = (
                    var, c["path"], c["duration_sec"], c["size_mb"], is_corrupted,
                )

                row = ttk.Frame(self._chunks_inner, style="Card.TFrame")
                row.pack(fill="x", anchor="e", pady=1)

                ttk.Label(
                    row, text=STATUS_LABELS[c["status"]],
                    foreground=STATUS_COLORS[c["status"]], background=PALETTE["card"],
                    width=16, anchor="w",
                ).pack(side="right", padx=6)

                duration_text = "⚠ مدة غير صالحة (ملف تالف)" if is_corrupted else _fmt_min_mb(
                    c["duration_sec"] / 60, c["size_mb"]
                )
                ttk.Label(
                    row, text=duration_text,
                    foreground=(PALETTE["danger"] if is_corrupted else PALETTE["text_muted"]),
                    background=PALETTE["card"],
                    width=26 if is_corrupted else 22, anchor="w",
                ).pack(side="right", padx=6)

                label_text = f"⚠ {c['filename']}" if is_corrupted else c["filename"]
                chk = ttk.Checkbutton(row, text=label_text, variable=var)
                chk.pack(side="right", padx=6, anchor="e")
                if is_corrupted:
                    _add_tooltip(
                        chk,
                        "مدة الملف طلعت غير منطقية - غالبًا الـ header اتقفل بشكل غير سليم.\n"
                        "جرب تصلحه برّه البرنامج: ffmpeg -i \"الملف\" -c copy fixed.flac",
                    )

        summary = pending_summary(lecture, state)
        if summary["count"] > 0:
            self.pending_badge.config(
                # النص كله إنجليزي بالكامل (مش خليط عربي/إنجليزي) عشان
                # ترتيب العرض مايتلخبطش جوه سياق RTL - راجع _fmt_min_mb
                text=(
                    f"⏳ Pending: {summary['count']} chunk(s) — "
                    f"{_fmt_min_mb(summary['total_minutes'], summary['total_mb'])}"
                )
            )
        else:
            self.pending_badge.config(text="")

        self._update_selection_total()

    def _update_selection_total(self):
        """بيحسب ويعرض إجمالي الدقايق والمساحة للأجزاء المحددة دلوقتي
        فقط (بغضّ النظر لو العملية اللي جاية هتفرّغ بس، تحوّل نوتس بس، أو
        الاتنين مع بعض) - عشان يبان دايمًا واضح إجمالي إيه اللي هيتعالج
        كوحدة واحدة قبل ما تضغط أي زرار."""
        if not hasattr(self, "selection_total_label"):
            return
        selected = [
            (dur, size) for var, _path, dur, size, _corrupted in self.chunk_vars.values() if var.get()
        ]
        if not selected:
            self.selection_total_label.config(text="مفيش أجزاء محددة")
            return
        total_minutes = sum(d for d, _ in selected) / 60
        total_mb = sum(s for _, s in selected)
        # النص كله إنجليزي بالكامل برضو، لنفس سبب شارة الأجزاء المعلقة فوق
        self.selection_total_label.config(
            text=f"📌 Selected: {len(selected)} chunk(s) — {_fmt_min_mb(total_minutes, total_mb)}"
        )

    def _select_all_chunks(self):
        for var, _path, _dur, _size, corrupted in self.chunk_vars.values():
            if not corrupted:  # مش بنحدد التالف تلقائي حتى بـ "تحديد الكل"
                var.set(True)

    def _deselect_all_chunks(self):
        for var, *_ in self.chunk_vars.values():
            var.set(False)

    def _get_selected_paths(self):
        return [path for var, path, *_ in self.chunk_vars.values() if var.get()]

    # ---------------------------------------------------------- Recording
    def _toggle_recording(self):
        if not self.recording:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self):
        lecture = self.current_lecture
        if not lecture:
            show_warning("تنبيه", "اختار أو أنشئ محاضرة/جلسة الأول.")
            return

        free_mb = check_disk_space_mb()
        if 0 <= free_mb < LOW_DISK_WARNING_MB:
            if not ask_yesno(
                "تحذير: مساحة قليلة",
                f"المساحة الفاضية على الديسك أقل من {LOW_DISK_WARNING_MB} ميجا "
                f"(المتاح دلوقتي: {free_mb:.0f} ميجا تقريباً).\n"
                "ده ممكن يوقف التسجيل فجأة لو خلصت المساحة أثناء جلسة طويلة.\n\n"
                "عايز تكمل برضو؟",
            ):
                return

        if not ask_yesno(
            "تأكيد التسجيل",
            f"هتسجل على المحاضرة/الجلسة:\n\n« {lecture} »\n\nمتأكد إن ده صح؟",
        ):
            return

        self.stop_flag.clear()
        self.recording = True
        self.paused = False
        self.record_btn.config(text="⏹️  إيقاف التسجيل", bg=PALETTE["neutral_bg"])
        self.record_btn._normal_bg = PALETTE["neutral_bg"]
        self.status_label.config(text="🔴 بيسجّل الآن...", foreground=PALETTE["danger"])
        self.lecture_combo.config(state="disabled")
        self.pause_btn.pack(side="right", padx=(0, 8))

        self._recording_start_time = time.monotonic()
        self._last_long_reminder_minutes = 0
        self._tick_timer()

        threading.Thread(target=self._capture_loop, daemon=True).start()
        self._write_thread = threading.Thread(target=self._write_loop, args=(lecture,), daemon=True)
        self._write_thread.start()

        self._log(f"بدأ التسجيل للمحاضرة/الجلسة: {lecture} (هيتقسم أوتوماتيك كل {CHUNK_MINUTES} min)")

    def _stop_recording(self):
        self.stop_flag.set()
        self.recording = False
        self.paused = False
        self._pause_started_at = None
        self.record_btn.config(text="▶️  ابدأ التسجيل", bg=PALETTE["danger"])
        self.record_btn._normal_bg = PALETTE["danger"]
        self.status_label.config(text="⏳ بيقفل ويضغط آخر جزء...", foreground=PALETTE["warning"])
        self.lecture_combo.config(state="readonly")
        self.pause_btn.pack_forget()
        self._log("جاري إيقاف التسجيل...")
        self._stop_timer()
        threading.Thread(target=self._wait_and_finish, daemon=True).start()

    def _toggle_pause(self):
        if not self.recording:
            return
        self.paused = not self.paused
        if self.paused:
            self._pause_started_at = time.monotonic()
            self.pause_btn.config(text="▶️ استكمال")
            self.status_label.config(text="⏸ متوقف مؤقتاً...", foreground=PALETTE["warning"])
            self._log("⏸ اتوقف التسجيل مؤقتاً (استراحة).")
        else:
            # بنزوّد وقت البداية بقد مدة الاستراحة، عشان مدة الاستراحة
            # متتحسبش ضمن وقت التسجيل الفعلي في العداد - من غير كده العداد
            # كان بيفضل يعد طول وقت الـ pause وكأن التسجيل مستمر عادي.
            if self._pause_started_at is not None and self._recording_start_time is not None:
                pause_duration = time.monotonic() - self._pause_started_at
                self._recording_start_time += pause_duration
            self._pause_started_at = None
            self.pause_btn.config(text="⏸ إيقاف مؤقت")
            self.status_label.config(text="🔴 بيسجّل الآن...", foreground=PALETTE["danger"])
            self._log("▶️ استكمل التسجيل بعد الاستراحة.")

    def _wait_and_finish(self):
        if self._write_thread is not None:
            self._write_thread.join()
        self.root.after(0, self.status_label.config, {"text": "✅ جاهز", "foreground": PALETTE["text_muted"]})
        self.root.after(0, self._log, "✅ التسجيل اتوقف وكل الأجزاء اتضغطت.")
        self.root.after(0, self._refresh_chunks)
        self.root.after(0, _beep)

    # ---------------------------------------------------------- Live timer
    def _tick_timer(self):
        if not self.recording or self._recording_start_time is None:
            return

        if self.paused:
            # العداد بيتجمّد وقت الاستراحة (العدد نفسه مش بيتغيّر) - بنكمل
            # نجدول tick تاني بس عشان لما اليوزر يكمل التسجيل يرجع يعد
            # عادي من غير ما نحتاج نبدأ التايمر تاني يدوي.
            self._elapsed_timer_job = self.root.after(1000, self._tick_timer)
            return

        elapsed = int(time.monotonic() - self._recording_start_time)
        hours, rem = divmod(elapsed, 3600)
        minutes, seconds = divmod(rem, 60)
        if hours > 0:
            text = f"⏱ {hours:02d}:{minutes:02d}:{seconds:02d}"
        else:
            text = f"⏱ {minutes:02d}:{seconds:02d}"
        self.timer_label.config(text=text)

        elapsed_minutes = elapsed // 60
        if (
            elapsed_minutes > 0
            and elapsed_minutes % LONG_RECORDING_REMINDER_MINUTES == 0
            and elapsed_minutes != self._last_long_reminder_minutes
        ):
            self._last_long_reminder_minutes = elapsed_minutes
            _beep()
            self._log(f"⏰ تذكير: التسجيل شغال من {elapsed_minutes // 60} ساعة تقريباً وما زال مستمرًا.")

        self._elapsed_timer_job = self.root.after(1000, self._tick_timer)

    def _stop_timer(self):
        if self._elapsed_timer_job is not None:
            self.root.after_cancel(self._elapsed_timer_job)
            self._elapsed_timer_job = None
        self._recording_start_time = None
        self.timer_label.config(text="")

    def _capture_loop(self):
        """
        بيلقط صوت النظام باستمرار، وكل ثانية (كل ما بيسجل numframes=SAMPLE_RATE
        وده بياخد ~ثانية) بيتأكد إن جهاز الإخراج الافتراضي لسه هو نفسه اللي
        بيسجل منه. لو الجهاز اتغيّر (سماعة راس اتفصلت/اتوصلت وبقت هي الافتراضية)
        بيقفل الـ recorder القديم بأمان ويفتح واحد جديد على الجهاز الجديد،
        من غير ما يوقف التسجيل كله أو يسيب فجوة صامتة تفضل من غير ما حد يلاحظ.
        """
        current_device_name = None
        mic = None
        recorder_ctx = None

        def _open_recorder_for_current_device():
            nonlocal current_device_name
            speaker = sc.default_speaker()
            new_mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            ctx = new_mic.recorder(samplerate=SAMPLE_RATE, channels=1)
            recorder = ctx.__enter__()
            current_device_name = speaker.name
            return ctx, recorder

        try:
            recorder_ctx, mic = _open_recorder_for_current_device()
            self._log_threadsafe(f"🎧 بيسجل من: {current_device_name}")

            while not self.stop_flag.is_set():
                # چيك خفيف كل دورة (~ثانية) - هل جهاز الإخراج الافتراضي اتغيّر؟
                try:
                    live_device_name = sc.default_speaker().name
                except Exception:
                    live_device_name = current_device_name  # فشل القراءة، نكمل بنفس الجهاز

                if live_device_name != current_device_name:
                    self._log_threadsafe(
                        f"⚠ جهاز الصوت الافتراضي اتغيّر ({current_device_name} ← {live_device_name})"
                        " - بيبدّل تلقائي من غير ما يوقف التسجيل"
                    )
                    try:
                        recorder_ctx.__exit__(None, None, None)
                    except Exception:
                        pass
                    try:
                        recorder_ctx, mic = _open_recorder_for_current_device()
                        self._log_threadsafe(f"🎧 اتبدل ويسجل دلوقتي من: {current_device_name}")
                    except Exception as e:
                        self._log_threadsafe(f"⚠ فشل التبديل للجهاز الجديد: {e} - بيحاول تاني")
                        time.sleep(1)
                        continue

                try:
                    data = mic.record(numframes=SAMPLE_RATE)
                    # وقت الإيقاف المؤقت، بنكمل نلقط من الجهاز (عشان نفضل
                    # نراقب تغيير الجهاز الافتراضي) بس مش بنحط في الكيو
                    # اللي بيتكتب فعلياً في الملف - يعني مفيش صوت استراحة
                    # بيتسجل، ومفيش فجوة صامتة تتكتب بدالها.
                    if not self.paused:
                        self.audio_queue.put(data.flatten().astype(np.float32))
                except Exception as e:
                    # الجهاز الحالي فشل فجأة (اتقفل/اتفصل) - حاول تعيد الاتصال
                    # بالـ default الحالي بدل ما التسجيل يقف بصمت
                    self._log_threadsafe(f"⚠ انقطاع مؤقت في التقاط الصوت ({e}) - بيحاول يعيد الاتصال")
                    try:
                        recorder_ctx.__exit__(None, None, None)
                    except Exception:
                        pass
                    time.sleep(0.5)
                    try:
                        recorder_ctx, mic = _open_recorder_for_current_device()
                        self._log_threadsafe(f"🎧 رجع يسجل من: {current_device_name}")
                    except Exception as e2:
                        self._log_threadsafe(f"⚠ لسه مش قادر يوصل لجهاز صوت: {e2}")
                        time.sleep(1)
        except Exception as e:
            self._log_threadsafe(f"⚠ خطأ في التقاط الصوت: {e}")
        finally:
            if recorder_ctx is not None:
                try:
                    recorder_ctx.__exit__(None, None, None)
                except Exception:
                    pass

    def _compress_chunk_background(self, flac_path):
        if ffmpeg_available():
            new_path = compress_to_opus(flac_path)
            self._log_threadsafe(f"تم ضغط جزء: {new_path.name}")
        else:
            self._log_threadsafe(f"ffmpeg مش متثبت، {flac_path.name} هيفضل FLAC. ثبّته بـ: winget install ffmpeg")
        self.root.after(0, self._refresh_chunks)

    def _write_loop(self, lecture: str):
        chunk_frames_limit = SAMPLE_RATE * 60 * CHUNK_MINUTES

        def new_path():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            return RECORD_FOLDER / f"{lecture}__{ts}.flac"

        current_path = new_path()
        frames_written = 0
        self._log_threadsafe(f"التسجيل هيتحفظ في: {current_path.name}")

        try:
            f = sf.SoundFile(str(current_path), mode="w", samplerate=SAMPLE_RATE, channels=1, format="FLAC")
            while not self.stop_flag.is_set() or not self.audio_queue.empty():
                try:
                    chunk = self.audio_queue.get(timeout=1)
                except queue.Empty:
                    continue

                f.write(chunk)
                frames_written += len(chunk)

                if frames_written >= chunk_frames_limit and not self.stop_flag.is_set():
                    f.close()
                    threading.Thread(target=self._compress_chunk_background, args=(current_path,), daemon=True).start()
                    current_path = new_path()
                    frames_written = 0
                    self._log_threadsafe(f"جزء جديد بدأ (بعد {CHUNK_MINUTES} min): {current_path.name}")
                    f = sf.SoundFile(str(current_path), mode="w", samplerate=SAMPLE_RATE, channels=1, format="FLAC")

            f.close()
            self._compress_chunk_background(current_path)

        except Exception as e:
            self._log_threadsafe(f"⚠ مشكلة في حفظ التسجيل: {e}")

    # ---------------------------------------------------------- Process
    def _start_processing(self, explain: bool):
        lecture = self.current_lecture
        if not lecture:
            show_warning("تنبيه", "اختار محاضرة/جلسة الأول.")
            return
        if self.recording:
            show_warning("تنبيه", "وقّف التسجيل الأول قبل ما تبدأ التفريغ.")
            return

        selected = self._get_selected_paths()
        if not selected:
            show_info("تنبيه", "مفيش أجزاء محددة. حدد جزء واحد على الأقل من القائمة.")
            return

        # نفصّل الملفات السليمة عن التالفة (مدة غير منطقية بسبب header
        # فاسد) - التالفة بتتستبعد من الإجمالي والمعالجة تلقائيًا، ولازم
        # اليوزر ياخد باله منها صراحة قبل ما يكمل، مش تتجمع بصمت مع رقم
        # مدة حقيقي وتبوظ كل الحسابات (تقدير التوكينز/التحذيرات) زي قبل.
        durations = {p: audio_duration_minutes_safe(p) for p in selected}
        corrupted = [p for p, d in durations.items() if d is None]
        clean = [p for p in selected if p not in corrupted]

        if corrupted:
            names = "\n".join(f"  • {p.name}" for p in corrupted)
            if not ask_yesno(
                "⚠ ملفات تالفة في التحديد",
                f"{len(corrupted)} ملف من المحدد مدته غير منطقية (على الأغلب "
                f"اتقفل بشكل غير سليم - جرب تصلحه بـ ffmpeg خارج البرنامج):\n\n"
                f"{names}\n\n"
                f"هيتم تجاهل الملفات دي والاستمرار بالباقي بس ({len(clean)} ملف). تكمل؟",
            ):
                return
            if not clean:
                show_info("تنبيه", "كل الملفات المحددة تالفة، مفيش حاجة تتعالج.")
                return

        total_minutes = sum(durations[p] for p in clean)
        total_mb = sum(p.stat().st_size for p in clean) / (1024 * 1024)
        action_desc = "التفريغ والتحويل لنوتس (كعملية واحدة مجمّعة)" if explain else "التفريغ فقط"
        warn = ""
        if len(clean) > 3 or total_minutes > 45:
            warn = "\n\n⚠ ده عدد/مدة كبيرة نسبياً، هياخد وقت ويستهلك استدعاءات API أكتر."

        # تقدير تقريبي بس (مش دقيق) لحجم النص المتوقع - مبني على معدل
        # كلام تقريبي (~130 كلمة/دقيقة عربي) عشان يديك فكرة عامة قبل
        # الموافقة، مش رقم مضمون من المزوّد.
        est_chars = int(total_minutes * 130 * 5.5)  # ~5.5 حرف/كلمة تقريبًا
        est_tokens = process_lecture.estimate_tokens_for_chars(est_chars)
        token_note = f"\n📊 تقدير تقريبي لحجم النص الناتج: ~{est_tokens:,} توكن (رقم تقريبي جداً، مش دقيق)"

        # جزء المدة/الحجم بالإنجليزي بالكامل زي باقي الأماكن (راجع _fmt_min_mb)
        if not ask_yesno(
            "تأكيد المعالجة",
            f"هتعمل «{action_desc}» لـ {len(clean)} جزء/أجزاء\n"
            f"Total: {_fmt_min_mb(total_minutes, total_mb)}{token_note}{warn}\n\nتكمل؟",
        ):
            return

        self._log(f"بدأ {action_desc} لـ {len(clean)} جزء/أجزاء (إجمالي {total_minutes:.1f} min)...")
        threading.Thread(target=self._process_worker, args=(lecture, clean, explain), daemon=True).start()

    def _begin_processing(self):
        self._processing = True
        self._cancel_processing_event.clear()
        self.root.after(0, self.btn_cancel_processing.pack, {"side": "left"})

    def _end_processing(self):
        self._processing = False
        self.root.after(0, self.btn_cancel_processing.pack_forget)

    def _cancel_processing(self):
        if not self._processing:
            return
        if ask_yesno("إلغاء", "هتوقف العملية الحالية بعد ما يخلص الجزء الشغال دلوقتي. متأكد؟"):
            self._cancel_processing_event.set()
            self._log("🛑 طلب إلغاء - هيوقف بعد ما يخلص الجزء الحالي...")

    def _process_worker(self, lecture: str, selected_paths: list, explain: bool):
        self._begin_processing()
        try:
            state = load_state(lecture)
            full_text = process_lecture.transcribe_files(
                lecture, state, selected_paths, delete_after_success=self.delete_after_var.get(),
            )
            if not full_text:
                self._log_threadsafe("مفيش نص متفرغ.")
                return

            if explain and not self._cancel_processing_event.is_set():
                process_lecture.summarize_new_part(lecture, full_text, state)
                self._log_threadsafe(f"✓ خلص! النوتس محفوظة في: {MARKDOWN_FOLDER / (lecture + '.md')}")
            elif not explain:
                self._log_threadsafe(f"✓ خلص التفريغ! النص الخام في: {TRANSCRIPT_FOLDER / (lecture + '.txt')}")
            self.root.after(0, _beep)

        except Exception as e:
            self._log_threadsafe(f"⚠ حصل خطأ أثناء المعالجة: {e}")
        finally:
            self._end_processing()
            self.root.after(0, self._refresh_chunks)
            self._progress_threadsafe(0, 0)

    # ---------------------------------------------------------- Notes-only (no re-transcribe)
    def _start_notes_only(self):
        lecture = self.current_lecture
        if not lecture:
            show_warning("تنبيه", "اختار محاضرة/جلسة الأول.")
            return

        transcript_path = TRANSCRIPT_FOLDER / f"{lecture}.txt"
        if not transcript_path.exists():
            show_info("تنبيه", "مفيش نص متفرغ لسه لهذه المحاضرة (فرّغ الأول).")
            return

        self._log("بدأ تحويل النص المفرّغ الموجود لنوتس (من غير تفريغ صوت إضافي)...")
        threading.Thread(target=self._notes_only_worker, args=(lecture, transcript_path), daemon=True).start()

    def _notes_only_worker(self, lecture: str, transcript_path):
        self._begin_processing()
        try:
            state = load_state(lecture)
            with open(transcript_path, "r", encoding="utf-8") as f:
                full_text = f.read()

            if not full_text.strip():
                self._log_threadsafe("النص الخام فاضي.")
                return

            process_lecture.summarize_new_part(lecture, full_text, state)
            self._log_threadsafe(f"✓ خلص! النوتس محفوظة في: {MARKDOWN_FOLDER / (lecture + '.md')}")
            self.root.after(0, _beep)

        except Exception as e:
            self._log_threadsafe(f"⚠ حصل خطأ أثناء تحويل النوتس: {e}")
        finally:
            self._end_processing()
            self.root.after(0, self._refresh_chunks)
            self._progress_threadsafe(0, 0)

    # ---------------------------------------------------------- Undo
    def _undo_last_update(self):
        lecture = self.current_lecture
        if not lecture:
            show_warning("تنبيه", "اختار محاضرة/جلسة الأول.")
            return

        if not ask_yesno(
            "تراجع عن آخر تحديث",
            "هيتشال آخر قسم نوتس اتضاف لملف الملخص، وترجع الأجزاء المرتبطة "
            "بيه لحالة \"متفرّغ\" (تقدر تحوّلها لنوتس تاني بعدين).\n\nمتأكد؟",
        ):
            return

        try:
            success = process_lecture.undo_last_notes_update(lecture)
            if success:
                remaining = len(load_state(lecture).get("_undo_stack", []))
                extra = f" (متبقي {remaining} خطوة/خطوات تراجع)" if remaining else " (مفيش خطوات تراجع تانية متاحة)"
                self._log(f"↩ اتلغى آخر تحديث نوتس للمحاضرة: {lecture}{extra}")
            else:
                self._log("مفيش تحديث سابق يتلغى لهذه المحاضرة.")
        except Exception as e:
            self._log(f"⚠ حصل خطأ أثناء التراجع: {e}")
        finally:
            self._refresh_chunks()

    # ---------------------------------------------------------- Selective delete
    def _open_delete_dialog(self):
        lecture = self.current_lecture
        if not lecture:
            show_warning("تنبيه", "اختار محاضرة/جلسة الأول.")
            return
        if self.recording:
            show_warning("تنبيه", "وقّف التسجيل الأول قبل ما تمسح بيانات المحاضرة.")
            return

        state = load_state(lecture)
        chunks = list_lecture_chunks(lecture, state)

        win = tk.Toplevel(self.root)
        win.title("مسح بيانات المحاضرة")
        win.configure(bg=PALETTE["card"])
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        win.geometry("780x580")

        ttk.Label(
            win, text=f"اختار بالظبط أي ريكورد وأي تفريغ عايز تمسحه من «{lecture}»:",
            background=PALETTE["card"], foreground=PALETTE["text"],
            font=("Segoe UI", 10, "bold"), justify="right", wraplength=720,
        ).pack(anchor="e", padx=16, pady=(16, 4))

        ttk.Label(
            win,
            text="🎙 = ملف الصوت الخام لهذا الجزء   |   📝 = التفريغ بتاعه بس "
                 "(متاح للأجزاء المتفرّغة اللي لسه مش متشرّحة فقط، لأن اللي "
                 "اتشرح بالفعل بُنيت النوتس عليه)",
            background=PALETTE["card"], foreground=PALETTE["text_muted"],
            font=("Segoe UI", 8), justify="right", wraplength=720,
        ).pack(anchor="e", padx=16, pady=(0, 8))

        # ---------------------------------------------------------------
        # جدول بأعمدة عرضها بكسل ثابت (مش width بالحروف زي قبل) - عشان
        # الهيدر والصفوف يتطابقوا بالظبط. width بالحروف بيختلف قياسه حسب
        # نوع الخط/محتوى كل خلية، فمكنش فيه ضمان تطابق فعلي - أما Frame
        # بعرض بكسل محدد مع pack_propagate(False) فبياخد نفس المساحة
        # بالظبط في كل مكان يتستخدم فيه، فالأعمدة بتتصاف صح مضمون.
        # ---------------------------------------------------------------
        COL_WIDTHS = {"tr_cb": 34, "audio_cb": 34, "status": 130, "duration": 150, "filename": 300}

        def _make_cell(parent, width, bg):
            cell = tk.Frame(parent, width=width, height=30, bg=bg)
            cell.pack_propagate(False)
            return cell

        list_outer = ttk.Frame(win, style="Card.TFrame")
        list_outer.pack(fill="both", expand=True, padx=16)

        header = tk.Frame(list_outer, bg=PALETTE["card"])
        header.pack(fill="x", pady=(0, 2))
        # ترتيب الأعمدة من اليمين لليسار (RTL): اسم الملف، المدة، الحالة،
        # شيك الصوت، شيك التفريغ - بنعبّي من side="right" بنفس الترتيب.
        for key, text in [
            ("filename", "اسم الملف"), ("duration", "المدة/الحجم"),
            ("status", "الحالة"), ("audio_cb", "🎙"), ("tr_cb", "📝"),
        ]:
            cell = _make_cell(header, COL_WIDTHS[key], PALETTE["card"])
            cell.pack(side="right")
            # أيقونات الهيدر (🎙/📝) بخط أكبر عشان تبان واضحة، مش نفس حجم
            # نص العناوين التانية اللي أصغر بطبيعتها
            is_icon = key in ("audio_cb", "tr_cb")
            tk.Label(
                cell, text=text, bg=PALETTE["card"], fg=PALETTE["text_muted"],
                font=("Segoe UI", 13 if is_icon else 8, "normal" if is_icon else "bold"),
            ).pack(expand=True)
        ttk.Separator(list_outer, orient="horizontal").pack(fill="x", pady=(0, 4))

        list_container = ttk.Frame(list_outer, style="Card.TFrame")
        list_container.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_container, highlightthickness=0, bg=PALETTE["card"], height=280)
        vsb = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, style="Card.TFrame")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        audio_vars: dict[str, tk.BooleanVar] = {}
        transcript_vars: dict[str, tk.BooleanVar] = {}

        if not chunks:
            ttk.Label(
                inner, text="مفيش أجزاء صوت مسجلة لهذه المحاضرة.",
                background=PALETTE["card"], foreground=PALETTE["text_muted"],
            ).pack(anchor="e", padx=4, pady=10)

        for row_i, c in enumerate(chunks):
            status_color = STATUS_COLORS.get(c["status"], PALETTE["text_muted"])
            status_text = STATUS_LABELS.get(c["status"], c["status"])
            duration_text = _fmt_min_mb(c["duration_sec"] / 60, c["size_mb"])

            row_bg = PALETTE["card"] if row_i % 2 == 0 else PALETTE["bg"]
            row = tk.Frame(inner, bg=row_bg)
            row.pack(fill="x")

            cell_fn = _make_cell(row, COL_WIDTHS["filename"], row_bg)
            cell_fn.pack(side="right")
            tk.Label(
                cell_fn, text=c["filename"], bg=row_bg, fg=PALETTE["text"],
                font=("Segoe UI", 9), anchor="e", justify="right",
            ).pack(fill="both", expand=True, padx=4)

            cell_dur = _make_cell(row, COL_WIDTHS["duration"], row_bg)
            cell_dur.pack(side="right")
            tk.Label(
                cell_dur, text=duration_text, bg=row_bg, fg=PALETTE["text_muted"],
                font=("Segoe UI", 9),
            ).pack(expand=True)

            cell_status = _make_cell(row, COL_WIDTHS["status"], row_bg)
            cell_status.pack(side="right")
            tk.Label(
                cell_status, text=status_text, bg=row_bg, fg=status_color,
                font=("Segoe UI", 9, "bold"),
            ).pack(expand=True)

            v_audio = tk.BooleanVar(value=False)
            audio_vars[c["filename"]] = v_audio
            cell_audio_cb = _make_cell(row, COL_WIDTHS["audio_cb"], row_bg)
            cell_audio_cb.pack(side="right")
            tk.Checkbutton(
                cell_audio_cb, variable=v_audio, bg=row_bg, activebackground=row_bg,
                highlightthickness=0, bd=0,
            ).pack(expand=True)

            cell_tr_cb = _make_cell(row, COL_WIDTHS["tr_cb"], row_bg)
            cell_tr_cb.pack(side="right")
            if c["status"] == "transcribed":
                v_tr = tk.BooleanVar(value=False)
                transcript_vars[c["filename"]] = v_tr
                tk.Checkbutton(
                    cell_tr_cb, variable=v_tr, bg=row_bg, activebackground=row_bg,
                    highlightthickness=0, bd=0,
                ).pack(expand=True)
            else:
                # فراغ بنفس عرض عمود الشيك بوكس عشان العمود يفضل متصاف
                # حتى للصفوف اللي مالهاش خيار تفريغ (متسجل بس أو متشرّح بالفعل)
                tk.Label(cell_tr_cb, text="—", bg=row_bg, fg=PALETTE["border"]).pack(expand=True)

        sep = ttk.Separator(win, orient="horizontal")
        sep.pack(fill="x", padx=16, pady=(12, 8))

        var_notes = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            win, text="🧠 امسح كل الملخص/النوتس (Markdown) للمحاضرة دي", variable=var_notes,
        ).pack(anchor="e", padx=16, pady=3)

        ttk.Label(
            win, text="⚠ المسح نهائي ومش هترجع فيه.",
            background=PALETTE["card"], foreground=PALETTE["danger"],
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="e", padx=16, pady=(10, 10))

        row_btns = ttk.Frame(win, style="Card.TFrame")
        row_btns.pack(fill="x", padx=16, pady=(0, 16))

        def _do_delete():
            chosen_audio = [fn for fn, v in audio_vars.items() if v.get()]
            chosen_transcript = [fn for fn, v in transcript_vars.items() if v.get()]
            del_notes = var_notes.get()

            if not chosen_audio and not chosen_transcript and not del_notes:
                show_info("تنبيه", "اختار حاجة واحدة على الأقل عشان تتمسح.")
                return

            parts_desc = []
            if chosen_audio:
                parts_desc.append(f"{len(chosen_audio)} ملف صوت")
            if chosen_transcript:
                parts_desc.append(f"تفريغ {len(chosen_transcript)} جزء")
            if del_notes:
                parts_desc.append("كل النوتس")

            if not self._confirm_destructive_by_typing(
                lecture,
                f"هتمسح نهائياً: {'، '.join(parts_desc)}.\nالمسح نهائي ومفيش نسخة احتياطية.",
            ):
                return

            win.destroy()
            self._log(f"بدأ مسح ({'، '.join(parts_desc)}) للمحاضرة: {lecture}")
            threading.Thread(
                target=self._delete_worker,
                args=(lecture, chosen_audio, chosen_transcript, del_notes),
                daemon=True,
            ).start()

        btn_confirm = self._card_button(
            row_btns, "🗑 امسح المحدد", _do_delete, PALETTE["danger"], PALETTE["danger_dark"],
        )
        btn_confirm.pack(side="right")

        btn_cancel = self._outline_button(
            row_btns, "إلغاء", win.destroy, PALETTE["text_muted"],
        )
        btn_cancel.pack(side="right", padx=(0, 8))

    def _confirm_destructive_by_typing(self, lecture: str, message: str) -> bool:
        """تأكيد إضافي للعمليات اللي مالهاش رجعة: بدل Yes/No بس (سهل جداً
        تدوسه بسرعة من غير ما تخد بالك)، لازم تكتب اسم المحاضرة بالظبط
        عشان تأكد إنك واخد بالك فعلاً إيه اللي هيتمسح نهائي. زرار "نسخ
        الاسم" موجود عشان اليوزر يلصقه علطول (Ctrl+V) بدل ما يكتبه يدوي
        ويغلط في حرف - المطابقة نص بنص فحرف زيادة/ناقص كفاية إنها ترفض."""
        win = tk.Toplevel(self.root)
        win.title("تأكيد نهائي")
        win.configure(bg=PALETTE["card"])
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        win.geometry("440x260")

        ttk.Label(
            win, text=message, background=PALETTE["card"], foreground=PALETTE["danger"],
            font=("Segoe UI", 10, "bold"), justify="right", wraplength=400,
        ).pack(anchor="e", padx=16, pady=(16, 10))

        ttk.Label(
            win, text="اكتب اسم المحاضرة بالظبط عشان تأكد:",
            background=PALETTE["card"], foreground=PALETTE["text"],
            justify="right", wraplength=400,
        ).pack(anchor="e", padx=16)

        row_name = ttk.Frame(win, style="Card.TFrame")
        row_name.pack(fill="x", padx=16, pady=(4, 12))

        def _copy_lecture_name():
            self.root.clipboard_clear()
            self.root.clipboard_append(lecture)
            self.root.update()

        btn_copy_name = self._outline_button(
            row_name, "📋 نسخ الاسم", _copy_lecture_name, PALETTE["text_muted"],
            padx=8, pady=4,
        )
        btn_copy_name.pack(side="left")
        _add_tooltip(btn_copy_name, "نسخ اسم المحاضرة بالظبط للـ clipboard عشان تلصقه (Ctrl+V) في الخانة تحت")

        name_label = ttk.Label(
            row_name, text=f"«{lecture}»", background=PALETTE["card"],
            foreground=PALETTE["accent_dark"], font=("Segoe UI", 9, "bold"),
            justify="right",
        )
        name_label.pack(side="right", fill="x", expand=True, padx=(0, 8))

        entry_var = tk.StringVar()
        entry = ttk.Entry(win, textvariable=entry_var, justify="right")
        entry.pack(fill="x", padx=16, pady=(0, 16))
        entry.focus_set()

        result = {"ok": False}

        def _confirm():
            if entry_var.get().strip() == lecture:
                result["ok"] = True
                win.destroy()
            else:
                show_warning("تنبيه", "الاسم مش مطابق - المسح اتلغى.")

        row = ttk.Frame(win, style="Card.TFrame")
        row.pack(fill="x", padx=16, pady=(0, 16))
        self._card_button(row, "🗑 تأكيد المسح", _confirm, PALETTE["danger"], PALETTE["danger_dark"]).pack(
            side="right"
        )
        self._outline_button(row, "إلغاء", win.destroy, PALETTE["text_muted"]).pack(
            side="right", padx=(0, 8)
        )

        entry.bind("<Return>", lambda e: _confirm())
        self.root.wait_window(win)
        return result["ok"]

    def _delete_worker(self, lecture: str, audio_files: list, transcript_files: list, del_notes: bool):
        try:
            parts = []

            if audio_files or transcript_files:
                report = delete_specific_files(
                    lecture, audio_filenames=audio_files, transcript_filenames=transcript_files,
                )
                if report["audio_deleted"]:
                    parts.append(f"{len(report['audio_deleted'])} ملف صوت")
                if report["transcript_deleted"]:
                    parts.append(f"تفريغ {len(report['transcript_deleted'])} جزء")
                if report["transcript_skipped_explained"]:
                    self._log_threadsafe(
                        "⚠ اتجاهل تفريغ الأجزاء دي لأنها اتشرحت بالفعل: "
                        + "، ".join(report["transcript_skipped_explained"])
                    )

            if del_notes:
                notes_report = delete_lecture_data(lecture, delete_notes=True)
                if notes_report["notes"]:
                    parts.append("ملف النوتس")

            if parts:
                self._log_threadsafe(f"✓ اتمسح: {'، '.join(parts)}")
            else:
                self._log_threadsafe("مفيش ملفات كانت موجودة أصلاً عشان تتمسح.")
        except Exception as e:
            self._log_threadsafe(f"⚠ حصل خطأ أثناء المسح: {e}")
        finally:
            self.root.after(0, self._refresh_chunks)

    # ---------------------------------------------------------- Notes viewer
    def _open_markdown_file(self):
        lecture = self.current_lecture
        md_path = MARKDOWN_FOLDER / f"{lecture}.md"
        if not md_path.exists():
            show_info("تنبيه", "مفيش ملف نوتس لسه لهذه المحاضرة.")
            return
        try:
            os.startfile(str(md_path))
        except Exception as e:
            show_error("خطأ", f"مقدرش أفتح الملف: {e}")

    def _show_notes_viewer(self):
        lecture = self.current_lecture
        md_path = MARKDOWN_FOLDER / f"{lecture}.md" if lecture else None
        if not lecture or not md_path.exists():
            show_info("تنبيه", "مفيش ملف نوتس لسه لهذه المحاضرة.")
            return

        win = tk.Toplevel(self.root)
        win.title(f"نوتس: {lecture}")
        win.geometry("820x680")

        toolbar = ttk.Frame(win, style="TFrame")
        toolbar.pack(fill="x", padx=8, pady=(6, 0))

        html_frame = HtmlFrame(win, messages_enabled=False)

        def _reload():
            self._render_notes_into(html_frame, lecture)

        btn_refresh = self._outline_button(toolbar, "🔄 تحديث", _reload, PALETTE["accent"])
        btn_refresh.pack(side="left")
        _add_tooltip(btn_refresh, "إعادة تحميل النوتس (لو اتحدثت وانت فاتح النافذة دي)")

        search_var = tk.StringVar()
        search_entry = ttk.Entry(toolbar, textvariable=search_var, width=24, justify="right")

        def _do_search(_event=None):
            term = search_var.get().strip()
            if not term:
                return
            finder = getattr(html_frame, "find_text", None)
            if finder is None:
                show_info("تنبيه", "البحث داخل النوتس مش مدعوم في نسخة tkinterweb الحالية.")
                return
            try:
                found = finder(term)
                if not found:
                    show_info("بحث", f"مفيش نتيجة لـ «{term}».")
            except Exception:
                show_info("تنبيه", "البحث داخل النوتس مش مدعوم في نسخة tkinterweb الحالية.")

        search_entry.bind("<Return>", _do_search)
        search_entry.pack(side="right", padx=(0, 4))
        self._outline_button(toolbar, "🔍 بحث", _do_search, PALETTE["text_muted"]).pack(
            side="right", padx=(0, 4)
        )
        # Ctrl+F يودّي الفوكس لحقل البحث بدل ما يتعمله بايند تاني على مستوى النافذة
        win.bind("<Control-f>", lambda e: search_entry.focus_set())
        win.bind("<Control-F>", lambda e: search_entry.focus_set())

        html_frame.pack(fill="both", expand=True)
        self._render_notes_into(html_frame, lecture)

    def _render_notes_into(self, html_frame, lecture: str):
        """يعيد قراءة ملف النوتس من الديسك ويحمّله في الـ html_frame -
        منفصلة عن _show_notes_viewer عشان زرار التحديث يقدر يستدعيها من
        غير ما يفتح نافذة جديدة."""
        md_path = MARKDOWN_FOLDER / f"{lecture}.md"
        if not md_path.exists():
            show_info("تنبيه", "مفيش ملف نوتس لسه لهذه المحاضرة.")
            return
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()

        content_with_math = render_math_to_html_images(content, fg_color="#1c1c1c")
        body = md_lib.markdown(content_with_math, extensions=["fenced_code", "tables", "nl2br"])
        body = _colorize_blockquotes(body)

        highlight_css = "\n".join(
            f'blockquote.{cls} {{ background: {bg}; border-right: 4px solid {border}; }}'
            for cls, bg, border in HIGHLIGHT_STYLES.values()
        )

        html = f"""<html dir="rtl" lang="ar">
<head><meta charset="utf-8">
<style>
body {{ direction: rtl; text-align: right; font-family: 'Segoe UI', Tahoma, sans-serif;
    padding: 20px; line-height: 1.9; font-size: 15px; }}
h1, h2, h3 {{ direction: rtl; text-align: right; border-bottom: 1px solid #ddd; padding-bottom: 6px; }}
p {{ direction: rtl; text-align: right; }}
strong, b {{ unicode-bidi: embed; }}
ul, ol {{ direction: rtl; text-align: right; padding-right: 22px; padding-left: 0;
    margin-right: 0; list-style-position: outside; }}
li {{ direction: rtl; text-align: right; margin-bottom: 6px; }}
code, pre {{ direction: ltr; text-align: left; unicode-bidi: embed; font-family: Consolas, monospace;
    background: #f4f4f4; border-radius: 4px; }}
pre {{ padding: 10px; overflow-x: auto; }}
code {{ padding: 2px 5px; }}
blockquote {{ direction: rtl; text-align: right; background: #f8f4e3; border-right: 4px solid #d4a017;
    margin: 10px 0; padding: 8px 14px; border-radius: 4px; }}
{highlight_css}
table {{ direction: rtl; border-collapse: collapse; width: 100%; margin: 12px 0; }}
td, th {{ border: 1px solid #ccc; padding: 6px 10px; text-align: right; }}
</style></head>
<body>{body}</body></html>"""

        try:
            html_frame.load_html(html)
        except Exception as e:
            show_error("خطأ", f"مقدرش أعرض النوتس: {e}")

    # ---------------------------------------------------------- Cleanup
    def _on_close(self):
        if self.recording:
            if not ask_yesno("تنبيه", "التسجيل لسه شغال. عايز تقفل فعلاً؟"):
                return
            self.stop_flag.set()
        elif self._processing:
            if not ask_yesno(
                "تنبيه",
                "فيه عملية تفريغ/تلخيص شغالة دلوقتي. لو قفلت البرنامج دلوقتي "
                "ممكن يضيع جزء من الشغل الحالي.\n\nعايز تقفل فعلاً؟",
            ):
                return
        self.root.destroy()


def main():
    root = tk.Tk()
    app = StudyApp(root)
    root.protocol("WM_DELETE_WINDOW", app._on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
