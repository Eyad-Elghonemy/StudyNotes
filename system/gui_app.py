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
from tkinter import messagebox, scrolledtext, ttk

import numpy as np
import soundcard as sc
import soundfile as sf
from dotenv import load_dotenv

import arabic_reshaper
from bidi.algorithm import get_display
import markdown as md_lib
from pygments.formatters import HtmlFormatter
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

APP_VERSION = "1.3.0"

SAMPLE_RATE = 16000
CHUNK_MINUTES = 30
LONG_RECORDING_REMINDER_MINUTES = 120  # تنبيه (مش إيقاف) كل ساعتين تسجيل مستمر

_PART_NUM_RE = re.compile(r"__Part (\d+)\.", re.IGNORECASE)


def _next_part_number(lecture: str) -> int:
    """
    بيدوّر على أعلى رقم "Part N" موجود بالفعل لملفات المحاضرة دي (بأي
    صيغة: flac/opus/wav) ويرجع الرقم اللي بعده، عشان الترقيم يكمل تسلسلي
    حتى لو المستخدم قفل البرنامج وسجل تاني بعدين.
    """
    highest = 0
    for ext in ("opus", "flac", "wav"):
        for p in RECORD_FOLDER.glob(f"{lecture}__Part *.{ext}"):
            m = _PART_NUM_RE.search(p.name)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


STATUS_LABELS = {
    "recorded": "متسجل بس",
    "transcribed": "متفرّغ",
    "explained": "متفرّغ ومتشرّح",
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
    "🔧": ("hl-correction", "#fdf1e3", "#e67e22"),   # تصحيح (اختياري)
    "💬": ("hl-addition", "#e8f8f5", "#16a085"),     # إضافة من المدوّن (اختياري)
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
    r"[A-Za-z0-9\(\)\[\]\-_./:%→←↔⚠✓✗🎧🔊🔴⏱⏳✅💡🗑↩📌📋]+"
    r"(?:[ \t]+[A-Za-z0-9\(\)\[\]\-_./:%→←↔]+)*"
)


def _isolate_ltr_runs(text: str) -> str:
    """يحوّط أي جزء لاتيني/رقمي/رمزي داخل السطر بعلامات LRI...PDI عشان
    خوارزمية bidi تعامله كوحدة منفصلة، بدل ما تدمجه جوه سياق العربي وتكسر
    ترتيب الأقواس/الأسهم اللي جواه."""
    return _NON_ARABIC_RUN_RE.sub(lambda m: f"{LRI}{m.group(0)}{PDI}", text)


_HEADPHONE_NAME_KEYWORDS = (
    "headphone", "headset", "earphone", "earbud", "airpods",
    "buds", "wh-", "سماعة", "سماعات",
)


def _device_type_emoji(device_name: str) -> str:
    """ايموجي تقريبي حسب نوع جهاز الإخراج الحالي، بناءً على اسمه: 🎧
    للسماعات (headphone/headset/earbuds...)، 🔊 لأي حاجة تانية (سبيكرز
    الجهاز، شاشة خارجية، إلخ). مفيش API في soundcard بيوضح نوع الجهاز
    الفعلي، فده أفضل تقريب متاح من الاسم بس."""
    name = (device_name or "").lower()
    if any(kw in name for kw in _HEADPHONE_NAME_KEYWORDS):
        return "🎧"
    return "🔊"


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
    APP_NAME = "StudyNotes"

    def __init__(self, root: tk.Tk):
        self.root = root
        self._update_title()
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
        self._active_recording_path = None  # مسار الملف اللي بيتكتب فيه دلوقتي فعليًا (مستبعد من قايمة الأجزاء لحد ما يتقفل)
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

        self._refresh_api_status_label()
        # نأجل الفحوصات دي شوية عشان الواجهة الرئيسية تكمل تظهر وترسم الأول
        # (خصوصًا نافذة إعداد أول تشغيل - المفروض تظهر فوق واجهة ظاهرة
        # بالفعل، مش قبلها).
        self.root.after(150, self._startup_checks)

    # ---------------------------------------------------------- Startup checks
    def _startup_checks(self):
        """فحوصات أول تشغيل: مفاتيح API، ffmpeg، جهاز صوت افتراضي - كل
        واحدة بتوريك تنبيه واضح أول ما البرنامج يفتح بدل ما تكتشفها بعد
        فشل عملية كاملة."""
        import first_run_setup
        if not first_run_setup.keys_configured():
            first_run_setup.show_dialog(self.root, PALETTE, on_done=self._on_first_run_setup_done)
            return  # النافذة دي إجبارية - باقي الفحوصات هتتنفذ بعدها لو كمّل

        if not ffmpeg_available():
            show_warning(
                "ffmpeg مش متثبت",
                "ffmpeg مش موجود على جهازك. البرنامج هيشتغل عادي، بس ملفات "
                "الصوت هتفضل FLAC (حجم أكبر بكتير) بدل ما تتضغط تلقائي لـ Opus.\n\n"
                "لتثبيته على ويندوز: افتح Terminal واكتب winget install ffmpeg",
            )

        try:
            sc.default_speaker()
        except Exception:
            show_warning(
                "مفيش جهاز صوت افتراضي",
                "مقدرش ألاقي جهاز إخراج صوت افتراضي على الجهاز ده. التسجيل "
                "مش هيشتغل صح لحد ما يبقى فيه جهاز صوت متوصل ومفعّل.",
            )

    def _on_first_run_setup_done(
        self, gemini_saved, groq_saved, gemini_rejected, groq_rejected,
        nvidia_saved=False, nvidia_rejected=False,
    ):
        self._refresh_api_status_label()
        saved = [n for n, ok in (
            ("Gemini", gemini_saved), ("Groq", groq_saved), ("NVIDIA", nvidia_saved),
        ) if ok]
        if saved:
            self._log(f"✓ تم حفظ مفتاح/مفاتيح API بنجاح: {'، '.join(saved)}")
        if gemini_rejected:
            self._log("⚠ مفتاح Gemini اللي دخلته مش صالح - اتجاهل. تقدر تضيفه لاحقًا في ملف .env يدويًا.")
        if groq_rejected:
            self._log("⚠ مفتاح Groq اللي دخلته مش صالح - اتجاهل. تقدر تضيفه لاحقًا في ملف .env يدويًا.")
        if nvidia_rejected:
            self._log("⚠ مفتاح NVIDIA اللي دخلته مش صالح - اتجاهل. تقدر تضيفه لاحقًا في ملف .env يدويًا.")
        # نكمّل باقي فحوصات أول تشغيل (ffmpeg / جهاز الصوت) بعد ما نافذة
        # المفاتيح تقفل
        self.root.after(100, self._startup_checks)

    def _refresh_api_status_label(self):
        gemini_ok = bool(process_lecture.GEMINI_API_KEY.strip())
        groq_ok = bool(process_lecture.GROQ_API_KEY.strip())
        nvidia_ok = bool(process_lecture.NVIDIA_API_KEY.strip())
        gemini_text = f"Gemini {'✓' if gemini_ok else '✗'}"
        groq_text = f"Groq {'✓' if groq_ok else '✗'}"
        parts = [gemini_text, groq_text]
        if nvidia_ok:
            # NVIDIA اختياري بالكامل - بيبان بس لو فعلاً متسجل، عشان منزحمش
            # الشريط بمفتاح تالت "✗" لكل يوزر مش مهتم بيه.
            parts.append("NVIDIA ✓")
        color = PALETTE["success"] if (gemini_ok or groq_ok) else PALETTE["danger"]
        self.api_status_label.config(text=f"🔑 {'  |  '.join(parts)}", foreground=color)

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

        # الصف ده كان فيه 5 عناصر مكدّسة جنب بعض في سطر واحد (قايمة
        # المحاضرات + زرار جديدة + badge الـ pending + حالة المفاتيح +
        # زرار الموديلات)، ومجموع عرضهم الطبيعي بيعدّي 1100px بسهولة -
        # أكبر من عرض الشاشة الافتراضي نفسه. النتيجة: العناصر كانت بتتقص
        # أو تختفي حتى في الحجم الافتراضي، مش بس لما الشاشة تتصغّر. الحل:
        # نقسّم لصفين - صف علوي لعناصر الجلسة (قايمة المحاضرات + جديدة)،
        # وصف تاني لحالة المفاتيح وزرار الموديلات - كل صف بمفرده بقى
        # محتاج مساحة أقل بكتير وثابت حتى مع تصغير الشاشة.
        top_row1 = ttk.Frame(frame_top, style="Card.TFrame")
        top_row1.pack(fill="x", padx=10, pady=(10, 4))

        top_row2 = ttk.Frame(frame_top, style="Card.TFrame")
        top_row2.pack(fill="x", padx=10, pady=(0, 10))

        self.lecture_var = tk.StringVar()
        self.lecture_combo = ttk.Combobox(
            top_row1, textvariable=self.lecture_var, state="readonly", width=42,
            justify="right",
        )
        self.lecture_combo.pack(side="right")
        self.lecture_combo.bind("<<ComboboxSelected>>", lambda e: self._on_lecture_change())

        new_lecture_btn = self._outline_button(
            top_row1, "➕ جديدة", self._new_lecture_dialog, PALETTE["accent"],
        )
        new_lecture_btn.pack(side="right", padx=10)
        _add_tooltip(new_lecture_btn, "إنشاء محاضرة/جلسة جديدة بالاسم")

        self.pending_badge = ttk.Label(top_row1, text="", style="Badge.TLabel", background=PALETTE["card"])
        self.pending_badge.pack(side="left", padx=2)

        # حالة مفاتيح الـ API الحالية - عشان المستخدم يعرف من أول نظرة لو
        # في مزوّد ناقص، بدل ما يكتشف بس وقت فشل عملية تفريغ/تلخيص
        self.api_status_label = ttk.Label(
            top_row2, text="", background=PALETTE["card"], font=("Segoe UI", 9, "bold"),
        )
        self.api_status_label.pack(side="left")
        _add_tooltip(self.api_status_label, "حالة مفاتيح Gemini/Groq/NVIDIA الحالية من ملف .env")

        settings_btn = self._card_button(
            top_row2, "⚙ الموديلات", self._show_model_settings_dialog,
            PALETTE["accent_soft"], PALETTE["accent_soft_dark"],
            font=("Segoe UI", 9, "bold"), padx=10, pady=5,
        )
        settings_btn.pack(side="left", padx=(10, 0))
        _add_tooltip(settings_btn, "Manually choose the transcription model and the summarization/notes model.")

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

        # الزرار الأهم بقى زرارين - كل واحد بياخد أسلوب مخرجات مختلف تمامًا
        # (راجع EXPLAIN_PROMPT مقابل MEETING_NOTES_PROMPT في process_lecture.py):
        # الأول لأسلوب "شرح محاضرة تعليمي" والتاني لأسلوب "محضر اجتماع مختصر".
        row_primary = ttk.Frame(frame_process, style="Card.TFrame")
        row_primary.pack(fill="x", padx=10, pady=(6, 3))
        row_primary.columnconfigure(0, weight=1)
        row_primary.columnconfigure(1, weight=1)

        self.btn_primary_lecture = self._card_button(
            row_primary, "🎓  فرّغ + لخص المحاضرة",
            lambda: self._start_processing(True, mode="lecture"),
            PALETTE["success"], PALETTE["success_dark"],
            font=("Segoe UI", 11, "bold"),
        )
        self.btn_primary_lecture.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        _add_tooltip(
            self.btn_primary_lecture,
            "Transcribe the selected chunks and turn them into organized lecture-style notes "
            "(headings, detail, full highlight boxes).",
        )

        self.btn_primary_meeting = self._card_button(
            row_primary, "🤝  فرّغ + خد نوتس",
            lambda: self._start_processing(True, mode="meeting"),
            PALETTE["info"], PALETTE["info_dark"],
            font=("Segoe UI", 11, "bold"),
        )
        self.btn_primary_meeting.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        _add_tooltip(
            self.btn_primary_meeting,
            "Transcribe the selected chunks and turn them into a short meeting summary "
            "(recap, decisions, action items, dates) instead of a detailed lecture explanation.",
        )

        # صف ثانوي: أفعال معالجة بديلة أقل استخداماً (تفريغ لوحده / نوتس
        # من نص موجود بالفعل) - التلاتة دلوقتي في صف واحد وبنفس لون العائلة
        # الهادئة (accent_soft) عشان يبانوا كمجموعة واحدة متناسقة، مختلفة
        # بصريًا عن زرارين الأهمية القصوى فوقهم.
        row_secondary = ttk.Frame(frame_process, style="Card.TFrame")
        row_secondary.pack(fill="x", padx=10, pady=4)
        row_secondary.columnconfigure(0, weight=1)
        row_secondary.columnconfigure(1, weight=1)
        row_secondary.columnconfigure(2, weight=1)

        btn_transcribe = self._card_button(
            row_secondary, "✍  فرّغ فقط",
            lambda: self._start_processing(False),
            PALETTE["accent_soft"], PALETTE["accent_soft_dark"],
            font=("Segoe UI", 9, "bold"),
            relief="ridge", bd=2, padx=8, pady=6,
        )
        btn_transcribe.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        _add_tooltip(btn_transcribe, "Transcribe the selected chunks to raw text only, without generating notes.")

        btn_notes_only = self._card_button(
            row_secondary, "🎓  لخص فقط",
            lambda: self._start_notes_only(mode="lecture"),
            PALETTE["accent_soft"], PALETTE["accent_soft_dark"],
            font=("Segoe UI", 9, "bold"),
            relief="ridge", bd=2, padx=8, pady=6,
        )
        btn_notes_only.grid(row=0, column=1, sticky="ew", padx=3)
        _add_tooltip(btn_notes_only, "Turn the existing transcript into lecture-style notes, without transcribing new audio.")

        btn_notes_only_meeting = self._card_button(
            row_secondary, "🤝  حوّل لنوتس بس",
            lambda: self._start_notes_only(mode="meeting"),
            PALETTE["accent_soft"], PALETTE["accent_soft_dark"],
            font=("Segoe UI", 9, "bold"),
            relief="ridge", bd=2, padx=8, pady=6,
        )
        btn_notes_only_meeting.grid(row=0, column=2, sticky="ew", padx=(3, 0))
        _add_tooltip(btn_notes_only_meeting, "Turn the existing transcript into a short meeting summary, without transcribing new audio.")

        # صف عرض النتيجة (Tier 3): أهم من زراير المسح/التراجع تحته، بس
        # أقل من زراير المعالجة فوقه - ألوان أهدأ برضه (soft) للتفرقة.
        default_app_name = _get_default_app_name(".md")

        row_view_notes = ttk.Frame(frame_process, style="Card.TFrame")
        row_view_notes.pack(fill="x", padx=10, pady=(2, 4))
        row_view_notes.columnconfigure(0, weight=1)
        row_view_notes.columnconfigure(1, weight=1)
        row_view_notes.columnconfigure(2, weight=1)

        btn_view_notes = self._card_button(
            row_view_notes, "📄  عرض النوتس هنا", self._show_notes_viewer,
            PALETTE["accent_soft"], PALETTE["accent_soft_dark"],
        )
        btn_view_notes.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        _add_tooltip(btn_view_notes, "Open a preview window for the notes (with equations rendered) inside the app.")

        btn_view_transcript = self._card_button(
            row_view_notes, "📃  عرض التفريغ هنا", self._show_transcript_viewer,
            PALETTE["accent_soft"], PALETTE["accent_soft_dark"],
        )
        btn_view_transcript.grid(row=0, column=1, sticky="ew", padx=3)
        _add_tooltip(btn_view_transcript, "Open a preview window for the raw transcript text, with a one-click copy button.")

        btn_open_md = self._card_button(
            row_view_notes, f"📂  افتح في {default_app_name}", self._open_markdown_file,
            PALETTE["info_soft"], PALETTE["info_soft_dark"],
        )
        btn_open_md.grid(row=0, column=2, sticky="ew", padx=(3, 0))
        _add_tooltip(btn_open_md, f"Open the Markdown file with your system's default app ({default_app_name}).")

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
        _add_tooltip(btn_delete, "Selectively delete lecture files (recording audio / transcript / notes) — this is permanent.")

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
        _add_tooltip(btn_undo, "Undo the last update: remove the last notes section added, and set its related chunks back to \"transcribed\".")

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
            frame_log, height=8, state="disabled", wrap="word",
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

    # ---------------------------------------------------------- Model settings
    def _show_model_settings_dialog(self):
        import first_run_setup
        win = tk.Toplevel(self.root)
        win.title("Model Settings")
        win.configure(bg=PALETTE["bg"])
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        # مرجع للنافذة دي عشان لو محتاجين نفك قفلها (grab) مؤقتًا وقت ما
        # نافذة تانية (زي "محتاج مفتاح") تتفتح فوقها - انظر
        # _prompt_for_missing_key_blocking لتفاصيل السبب.
        self._model_settings_win = win
        win.protocol("WM_DELETE_WINDOW", lambda: (setattr(self, "_model_settings_win", None), win.destroy()))

        WIDTH = 460

        # ---------- Header ----------
        header = tk.Frame(win, bg=PALETTE["accent"], width=WIDTH)
        header.pack(fill="x")
        header.pack_propagate(False)
        header.configure(height=64)
        tk.Label(
            header, text="⚙  Model Settings", bg=PALETTE["accent"], fg="white",
            font=("Segoe UI", 14, "bold"), anchor="w",
        ).pack(side="left", padx=20, pady=14)

        body = tk.Frame(win, bg=PALETTE["bg"])
        body.pack(fill="both", expand=True, padx=20, pady=(16, 8))

        tk.Label(
            body,
            text="Pick the model you prefer for each task. If it times out or "
                 "fails, the app automatically falls back to the other one for you.",
            bg=PALETTE["bg"], fg=PALETTE["text_muted"],
            font=("Segoe UI", 9), justify="left", wraplength=WIDTH - 40,
        ).pack(anchor="w", pady=(0, 14))

        def _provider_for_choice(choices_dict, key):
            """بترجع اسم المزوّد ("gemini"/"groq"/"nvidia") من مفتاح
            الاختيار، أو None لو الاختيار "auto" (مفيهوش مزوّد واحد محدد)."""
            entry = choices_dict.get(key)
            if not entry or entry[1] is None:
                return None
            return entry[1][0]

        def _add_choice_card(icon, title, subtitle, accent_color, choices_dict, current_key, on_change, last_used_getter=None, default_auto_provider=None):
            card = tk.Frame(
                body, bg=PALETTE["card"], highlightthickness=1,
                highlightbackground=PALETTE["border"], highlightcolor=PALETTE["border"],
            )
            card.pack(fill="x", pady=(0, 12))

            # colored accent strip on the left of the card
            strip = tk.Frame(card, bg=accent_color, width=5)
            strip.pack(side="left", fill="y")

            inner = tk.Frame(card, bg=PALETTE["card"])
            inner.pack(side="left", fill="both", expand=True, padx=16, pady=14)

            title_row = tk.Frame(inner, bg=PALETTE["card"])
            title_row.pack(fill="x", anchor="w")
            tk.Label(
                title_row, text=icon, bg=PALETTE["card"], fg=accent_color,
                font=("Segoe UI", 14),
            ).pack(side="left")
            tk.Label(
                title_row, text=title, bg=PALETTE["card"], fg=PALETTE["text"],
                font=("Segoe UI", 10, "bold"),
            ).pack(side="left", padx=(8, 0))

            tk.Label(
                inner, text=subtitle, bg=PALETTE["card"], fg=PALETTE["text_muted"],
                font=("Segoe UI", 8), justify="left",
            ).pack(anchor="w", pady=(2, 8))

            display_to_key = {v[0]: k for k, v in choices_dict.items()}
            var = tk.StringVar(value=choices_dict[current_key][0])

            # صف اللوجو + الـ dropdown جنب بعض. اللوجو بيتغيّر تلقائي حسب
            # المزوّد بتاع الاختيار الحالي (ولا بيظهر خالص لو الاختيار
            # "Auto" - مفيش مزوّد واحد محدد نعرضله لوجو).
            combo_row = tk.Frame(inner, bg=PALETTE["card"])
            combo_row.pack(anchor="w", fill="x")

            logo_label = tk.Label(combo_row, bg=PALETTE["card"])
            logo_label.pack(side="left", padx=(0, 6))

            def _refresh_logo(key):
                provider = _provider_for_choice(choices_dict, key)
                if provider is None:
                    # "Auto" - مفيش مزوّد واحد ثابت مختار يدويًا. بنفضّل
                    # نعرض لوجو آخر مزوّد نجح فعليًا في الجلسة الحالية
                    # (last_used_getter) لو موجود، وإلا بنعرض لوجو أول
                    # مزوّد في سلسلة الـ fallback الافتراضية
                    # (default_auto_provider - Groq للتفريغ، Gemini
                    # للتلخيص) عشان اللوجو يبان من غير ما يستنى أي تشغيل،
                    # لأنه فعليًا ده اللي هيتجرب الأول.
                    provider = (last_used_getter() if last_used_getter else None) or default_auto_provider
                    if provider is None:
                        logo_label.configure(image="", text="🔄", font=("Segoe UI", 11))
                        logo_label.image = None
                        return
                photo = first_run_setup._load_logo(
                    first_run_setup.PROVIDER_INFO.get(provider, {}).get("logo", ""), size=18,
                )
                if photo is None:
                    logo_label.configure(image="", text="🔄", font=("Segoe UI", 11))
                    logo_label.image = None
                else:
                    logo_label.configure(image=photo, text="")
                    logo_label.image = photo  # مرجع يمنع الـ garbage collection

            _refresh_logo(current_key)

            combo = ttk.Combobox(
                combo_row, textvariable=var, values=list(display_to_key.keys()),
                state="readonly", width=40, justify="left",
                style="Settings.TCombobox",
            )
            combo.pack(side="left", fill="x", expand=True)

            def _on_select(event):
                selected_key = display_to_key[var.get()]
                _refresh_logo(selected_key)
                on_change(selected_key)

            combo.bind("<<ComboboxSelected>>", _on_select)
            return var

        style = ttk.Style(win)
        style.configure("Settings.TCombobox", padding=6)

        self._transcribe_model_var = _add_choice_card(
            "🎙", "Speech-to-Text Model", "Used to transcribe recorded audio into raw text.",
            PALETTE["info"],
            process_lecture.TRANSCRIBE_MODEL_CHOICES,
            process_lecture.TRANSCRIBE_MODEL_CHOICE,
            self._on_transcribe_model_changed,
            last_used_getter=lambda: process_lecture.LAST_USED_TRANSCRIBE_PROVIDER,
            default_auto_provider="groq",  # أول مزوّد في سلسلة fallback التفريغ الافتراضية
        )

        self._summary_model_var = _add_choice_card(
            "🧠", "Summarization / Notes Model", "Used to turn the transcript into organized notes.",
            PALETTE["success"],
            process_lecture.SUMMARY_MODEL_CHOICES,
            process_lecture.SUMMARY_MODEL_CHOICE,
            self._on_summary_model_changed,
            last_used_getter=lambda: process_lecture.LAST_USED_SUMMARY_PROVIDER,
            default_auto_provider="gemini",  # أول مزوّد في سلسلة fallback التلخيص الافتراضية
        )

        tk.Label(
            body,
            text="Changes apply and save instantly (to .env) — no restart needed.",
            bg=PALETTE["bg"], fg=PALETTE["text_muted"],
            font=("Segoe UI", 8, "italic"), justify="left", wraplength=WIDTH - 40,
        ).pack(anchor="w", pady=(2, 4))

        footer = tk.Frame(win, bg=PALETTE["bg"])
        footer.pack(fill="x", padx=20, pady=(8, 18))

        def _close_model_settings():
            self._model_settings_win = None
            win.destroy()

        close_btn = self._card_button(
            footer, "Done", _close_model_settings, PALETTE["accent"], PALETTE["accent_dark"],
        )
        close_btn.pack(side="right")

        win.update_idletasks()
        w = win.winfo_reqwidth() + 4
        h = win.winfo_reqheight() + 4
        x = self.root.winfo_x() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - h) // 2
        screen_w, screen_h = win.winfo_screenwidth(), win.winfo_screenheight()
        x = max(0, min(x, screen_w - w))
        y = max(0, min(y, screen_h - h))
        win.geometry(f"{w}x{h}+{x}+{y}")

    def _required_provider_for_choice(self, choice_key: str, choices_dict: dict):
        """يرجع اسم المزوّد (provider) اللي الاختيار ده محتاجه، أو None
        لو Auto (مش محتاج تحقق من مفتاح معيّن)."""
        if choice_key == "auto":
            return None
        entry = choices_dict.get(choice_key)
        if not entry or not entry[1]:
            return None
        return entry[1][0]

    def _provider_key_configured(self, provider: str) -> bool:
        return bool({
            "gemini": process_lecture.GEMINI_API_KEY,
            "groq": process_lecture.GROQ_API_KEY,
            "nvidia": process_lecture.NVIDIA_API_KEY,
        }.get(provider, "").strip())

    def _prompt_for_missing_key_blocking(self, provider: str) -> bool:
        """بتفتح نافذة توجيه المفتاح وتستنى (modal) لحد ما تتقفل، وترجع
        True لو المستخدم حفظ مفتاح صالح، False لو لغى.

        لو النافذة دي بتتفتح من جوه نافذة "Model Settings" (اللي ماسكة
        قفل إدخال - grab - بالفعل)، لازم نفك القفل ده مؤقتًا الأول -
        نافذتين بيحاولوا ياخدوا القفل الحصري في نفس الوقت بيلخبطوا
        Tkinter ويخلوا النافذة التانية تتجمد نص رسم (تظهر جزء بس من
        محتواها، زي ما لو الأزرار اختفت). بعد ما اليوزر يخلص، بنرجّع
        القفل لنافذة Model Settings تاني لو لسه مفتوحة.
        """
        import first_run_setup

        model_settings_win = getattr(self, "_model_settings_win", None)
        had_grab = False
        if model_settings_win is not None and model_settings_win.winfo_exists():
            try:
                model_settings_win.grab_release()
                had_grab = True
            except tk.TclError:
                pass

        result = {"saved": False}

        def _on_done(saved):
            result["saved"] = saved

        # لو النافذة دي طالعة فوق Model Settings، خليها تتوسّط وتترتب
        # (transient) بالنسبة لها هي، مش للنافذة الرئيسية اللي وراها -
        # عشان الترتيب البصري يبقى منطقي (فوق النافذة اللي فتحتها فعليًا).
        parent_win = model_settings_win if (model_settings_win is not None and model_settings_win.winfo_exists()) else self.root
        dlg = first_run_setup.prompt_for_missing_key(parent_win, PALETTE, provider, on_done=_on_done)
        if dlg is not None:
            self.root.wait_window(dlg.win)

        if had_grab and model_settings_win.winfo_exists():
            try:
                model_settings_win.grab_set()
            except tk.TclError:
                pass

        return result["saved"]

    def _on_transcribe_model_changed(self, choice_key: str):
        provider = self._required_provider_for_choice(choice_key, process_lecture.TRANSCRIBE_MODEL_CHOICES)
        if provider and not self._provider_key_configured(provider):
            saved = self._prompt_for_missing_key_blocking(provider)
            if not saved:
                # المستخدم لغى - نرجع الاختيار لـ Auto بدل ما يفضل عالق
                # على موديل مش شغال، ونحدّث شكل الـ dropdown نفسه كمان.
                choice_key = "auto"
                if hasattr(self, "_transcribe_model_var"):
                    self._transcribe_model_var.set(process_lecture.TRANSCRIBE_MODEL_CHOICES["auto"][0])
                self._log("🎙 اتلغى - رجع موديل التفريغ لـ Auto.")

        process_lecture.set_transcribe_model_choice(choice_key)
        self._log(f"🎙 موديل التفريغ اتغيّر لـ: {process_lecture.TRANSCRIBE_MODEL_CHOICES[choice_key][0]}")

    def _on_summary_model_changed(self, choice_key: str):
        provider = self._required_provider_for_choice(choice_key, process_lecture.SUMMARY_MODEL_CHOICES)
        if provider and not self._provider_key_configured(provider):
            saved = self._prompt_for_missing_key_blocking(provider)
            if not saved:
                choice_key = "auto"
                if hasattr(self, "_summary_model_var"):
                    self._summary_model_var.set(process_lecture.SUMMARY_MODEL_CHOICES["auto"][0])
                self._log("🧠 اتلغى - رجع موديل التلخيص لـ Auto.")

        process_lecture.set_summary_model_choice(choice_key)
        self._log(f"🧠 موديل التلخيص اتغيّر لـ: {process_lecture.SUMMARY_MODEL_CHOICES[choice_key][0]}")

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
        win = tk.Toplevel(self.root)
        win.title("محاضرة/جلسة جديدة")
        win.configure(bg=PALETTE["bg"])
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        WIDTH = 420
        header = tk.Frame(win, bg=PALETTE["accent"], width=WIDTH)
        header.pack(fill="x")
        header.pack_propagate(False)
        header.configure(height=52)
        tk.Label(
            header, text="➕  محاضرة/جلسة جديدة", bg=PALETTE["accent"], fg="white",
            font=("Segoe UI", 13, "bold"), anchor="e", justify="right",
        ).pack(fill="x", padx=20, pady=13)

        body = tk.Frame(win, bg=PALETTE["bg"])
        body.pack(fill="both", expand=True, padx=20, pady=(16, 8))

        ttk.Label(
            body, text="اسم المحاضرة أو الجلسة:", background=PALETTE["bg"],
            foreground=PALETTE["text"], font=("Segoe UI", 9, "bold"),
        ).pack(anchor="e")
        name_var = tk.StringVar()
        name_entry = ttk.Entry(body, textvariable=name_var, width=40, justify="right")
        name_entry.pack(anchor="e", fill="x", pady=(4, 14))
        name_entry.focus_set()

        ttk.Label(
            body, text="مجال المحاضرة (اختياري):", background=PALETTE["bg"],
            foreground=PALETTE["text"], font=("Segoe UI", 9, "bold"),
        ).pack(anchor="e")
        ttk.Label(
            body,
            text="بيساعد في تخصيص أسلوب وقواعد التلخيص لطبيعة مادتك تحديدًا "
                 "(مصطلحات، دقة الأرقام، صيغة المعادلات...إلخ).",
            background=PALETTE["bg"], foreground=PALETTE["text_muted"],
            font=("Segoe UI", 8), justify="right", wraplength=WIDTH - 40,
        ).pack(anchor="e", pady=(2, 6))

        subject_options = [process_lecture.DEFAULT_SUBJECT_LABEL] + list(
            process_lecture.SUBJECT_PROFILES.keys()
        ) + ["أخرى..."]
        subject_var = tk.StringVar(value=process_lecture.DEFAULT_SUBJECT_LABEL)
        subject_combo = ttk.Combobox(
            body, textvariable=subject_var, values=subject_options,
            state="readonly", width=38, justify="right",
        )
        subject_combo.pack(anchor="e", fill="x")

        other_var = tk.StringVar()
        other_entry = ttk.Entry(body, textvariable=other_var, width=40, justify="right")

        def _on_subject_changed(*_):
            if subject_var.get() == "أخرى...":
                other_entry.pack(anchor="e", fill="x", pady=(6, 0))
            else:
                other_entry.pack_forget()

        subject_combo.bind("<<ComboboxSelected>>", _on_subject_changed)

        ttk.Separator(body, orient="horizontal").pack(fill="x", pady=(14, 10))

        ttk.Label(
            body, text="خيارات إضافية للنوتس (اختياري):", background=PALETTE["bg"],
            foreground=PALETTE["text"], font=("Segoe UI", 9, "bold"),
        ).pack(anchor="e")
        ttk.Label(
            body,
            text="بتتفعّل بس لو حبيت، ونادرًا ما بتظهر حتى لو مفعّلة - أغلب "
                 "المحاضرات مش هتحتاجهم، وده طبيعي 100%.",
            background=PALETTE["bg"], foreground=PALETTE["text_muted"],
            font=("Segoe UI", 8), justify="right", wraplength=WIDTH - 40,
        ).pack(anchor="e", pady=(2, 8))

        corrections_var = tk.BooleanVar(value=False)
        additions_var = tk.BooleanVar(value=False)

        def _build_option_row(var, icon, icon_color, text, tooltip):
            # الإيموجي بيتحط في Label منفصل (مش جوه نص الـ Checkbutton
            # نفسه) - Tkinter بيرندر إيموجي داخل نص الـ Checkbutton بشكل
            # مكسور أحيانًا على ويندوز، لكن جوه Label عادي بيشتغل تمام.
            row = tk.Frame(body, bg=PALETTE["bg"])
            row.pack(fill="x", anchor="e", pady=(0, 6))
            check = tk.Checkbutton(
                row, variable=var, bg=PALETTE["bg"], activebackground=PALETTE["bg"],
                selectcolor=PALETTE["card"], cursor="hand2",
            )
            check.pack(side="right")
            icon_label = tk.Label(row, text=icon, bg=PALETTE["bg"], fg=icon_color, font=("Segoe UI", 12))
            icon_label.pack(side="right", padx=(0, 4))
            text_label = tk.Label(
                row, text=text, bg=PALETTE["bg"], fg=PALETTE["text"],
                font=("Segoe UI", 9), justify="right", anchor="e", wraplength=WIDTH - 80,
            )
            text_label.pack(side="right", fill="x", expand=True)
            for w in (check, icon_label, text_label):
                _add_tooltip(w, tooltip)

        _build_option_row(
            corrections_var, "🔧", "#e67e22",
            "صحّح المعلومات الغلط بوضوح (لو المحاضر قال حاجة غلط فعلاً)",
            "بيضيف صندوق 🔧 بس لو المحاضر قال معلومة غلط factually بشكل "
            "مؤكد (رقم غلط، قانون علمي خطأ، تسمية غلط) - مش لمجرد رأي أو "
            "تبسيط متعمد.",
        )
        _build_option_row(
            additions_var, "💬", "#16a085",
            "اسمح بإضافات بسيطة ومفيدة من معرفة عامة",
            "بيضيف صندوق 💬 بس لو فيه معلومة قصيرة هتوضّح نقطة المحاضر "
            "فعليًا - مش حشو أو تكرار لحاجة اتقالت بالفعل.",
        )

        footer = tk.Frame(win, bg=PALETTE["bg"])
        footer.pack(fill="x", padx=20, pady=(14, 18))

        def _create():
            name = name_var.get().strip()
            if not name:
                messagebox.showwarning("تنبيه", "لازم تكتب اسم للمحاضرة/الجلسة.", parent=win)
                return

            chosen = subject_var.get()
            if chosen == "أخرى...":
                custom = other_var.get().strip()
                subject = custom if custom else ""
            elif chosen == process_lecture.DEFAULT_SUBJECT_LABEL:
                subject = ""  # الافتراضي = فاضي، عشان يفضل زي السلوك القديم بالظبط
            else:
                subject = chosen

            clean = safe_name(name)
            state = load_state(clean)
            state["subject"] = subject
            state["enable_corrections"] = corrections_var.get()
            state["enable_additions"] = additions_var.get()
            save_state(clean, state)

            values = list(self.lecture_combo["values"])
            if clean not in values:
                self.lecture_combo["values"] = values + [clean]
            self.lecture_combo.set(clean)
            self._on_lecture_change()
            win.destroy()

        create_btn = self._card_button(
            footer, "✓ إنشاء", _create, PALETTE["accent"], PALETTE["accent_dark"],
        )
        create_btn.pack(side="left")
        self._outline_button(footer, "إلغاء", win.destroy, PALETTE["text_muted"]).pack(side="right")

        name_entry.bind("<Return>", lambda e: _create())

        win.update_idletasks()
        w, h = win.winfo_reqwidth() + 4, win.winfo_reqheight() + 4
        x = self.root.winfo_x() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - h) // 2
        screen_w, screen_h = win.winfo_screenwidth(), win.winfo_screenheight()
        x = max(0, min(x, screen_w - w))
        y = max(0, min(y, screen_h - h))
        win.geometry(f"{w}x{h}+{x}+{y}")

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
        # الملف اللي بيتسجل فيه دلوقتي فعليًا (لسه مفتوح) مستبعد تمامًا من
        # القايمة - مش نعرضه كـ"تالف"، لأنه أصلاً لسه ملقفلش ومفيش داعي
        # يظهر خالص لحد ما يتقفل ويتم تسجيله بشكل نهائي.
        active_path = getattr(self, "_active_recording_path", None)
        if active_path is not None:
            chunks = [c for c in chunks if c["path"] != active_path]

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

    def _end_chars_for_selection(self, state: dict, selected_paths: list):
        """
        بيرجع أقصى نهاية نطاق (offset) في ملف الترانسكريبت التراكمي بتاع
        الأجزاء المحددة بس، عشان التلخيص يقف عند حدود التحديد بدل ما ياخد
        كل النص الجديد المتراكم زيادة عن المطلوب (خصوصًا مهم لما فيه نص
        قديم لسه مش متلخص من محاولات سابقة فشلت). لو مفيش تحديد أو مفيش
        بيانات مدى، بيرجع None (يعني "خد لحد آخر النص" - السلوك الافتراضي).
        """
        if not selected_paths:
            return None
        ranges = state.get("transcript_ranges", {})
        ends = [ranges[p.name][1] for p in selected_paths if p.name in ranges]
        return max(ends) if ends else None

    # ---------------------------------------------------------- Title / status badge
    def _update_title(self):
        """
        بيحدّث عنوان النافذة (اللي بيبان في شريط العنوان وفي الـ taskbar
        preview) عشان يعكس حالة التسجيل فورًا - نقطة حمراء ● وقت التسجيل
        الفعلي، وأيقونة ⏸ وقت الإيقاف المؤقت، من غير أي بادج. ده أبسط حل
        متوافق 100% مع ويندوز (تغيير أيقونة الـ exe نفسها وقت التشغيل مش
        مدعوم مباشرة من tkinter/ويندوز من غير مكتبات خارجية إضافية).
        """
        if getattr(self, "recording", False):
            status = "⏸ Paused" if getattr(self, "paused", False) else "🔴 Recording"
            self.root.title(f"{self.APP_NAME} — {status}")
        else:
            self.root.title(self.APP_NAME)

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
        self._update_title()
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
        self._update_title()
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
        self._update_title()
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
            self._log_threadsafe(f"{_device_type_emoji(current_device_name)} بيسجل من: {current_device_name}")

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
                        self._log_threadsafe(f"{_device_type_emoji(current_device_name)} اتبدل ويسجل دلوقتي من: {current_device_name}")
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
                        self._log_threadsafe(f"{_device_type_emoji(current_device_name)} رجع يسجل من: {current_device_name}")
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

        # رقم الجزء (Part N) بيتحسب من أعلى رقم موجود بالفعل لنفس المحاضرة
        # + 1 (مش من الصفر)، عشان لو المستخدم وقف وسجل تاني بعدين، الترقيم
        # يكمل تسلسلي وميبوظش أو يتكرر مع أجزاء قديمة.
        next_part = _next_part_number(lecture)

        def new_path():
            nonlocal next_part
            n = next_part
            next_part += 1
            return RECORD_FOLDER / f"{lecture}__Part {n:02d}.flac"

        current_path = new_path()
        # الملف ده لسه مفتوح وبيتكتب فيه دلوقتي - لازم نستثنيه من قايمة
        # الأجزاء في الواجهة، وإلا sf.info() هيقرا header غير مكتمل ويظهره
        # غلط كـ"ملف تالف" لحد ما يتقفل فعليًا.
        self._active_recording_path = current_path
        frames_written = 0
        self._log_threadsafe(f"التسجيل هيتحفظ في: {current_path.name}")

        f = sf.SoundFile(str(current_path), mode="w", samplerate=SAMPLE_RATE, channels=1, format="FLAC")
        try:
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
                    self._active_recording_path = current_path
                    frames_written = 0
                    self._log_threadsafe(f"جزء جديد بدأ (بعد {CHUNK_MINUTES} min): {current_path.name}")
                    f = sf.SoundFile(str(current_path), mode="w", samplerate=SAMPLE_RATE, channels=1, format="FLAC")

        except Exception as e:
            self._log_threadsafe(f"⚠ مشكلة في حفظ التسجيل: {e}")
        finally:
            # لازم نتأكد من قفل الملف دايماً حتى لو حصل استثناء نص الكتابة
            # (زي امتلاء الديسك) - وإلا الملف بيفضل مقفول من نظام التشغيل
            # ومينفعش يتضغط أو يتفرّغ بعد كده من غير إعادة تشغيل البرنامج.
            try:
                f.close()
            except Exception:
                pass
            self._active_recording_path = None
            self._compress_chunk_background(current_path)

    # ---------------------------------------------------------- Process
    def _start_processing(self, explain: bool, mode: str = "lecture"):
        lecture = self.current_lecture
        if not lecture:
            show_warning("تنبيه", "اختار محاضرة/جلسة الأول.")
            return
        if self.recording:
            show_warning("تنبيه", "وقّف التسجيل الأول قبل ما تبدأ التفريغ.")
            return
        if self._processing:
            show_warning("تنبيه", "فيه عملية تفريغ/تلخيص شغالة بالفعل - استنى تخلص الأول.")
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
        threading.Thread(target=self._process_worker, args=(lecture, clean, explain, mode), daemon=True).start()

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

    def _maybe_suggest_nvidia_after_drop(self):
        """
        لو حصل توقف جزئي (drop) في التلخيص، وNVIDIA مش متسجل، بنقترح
        عليه يضيفه كـ fallback تالت - مرة واحدة بس لكل جلسة تشغيل، عشان
        منزعجوش بنفس الاقتراح كل مرة يحصل فيها drop.
        """
        if getattr(self, "_nvidia_suggested_this_session", False):
            return
        if process_lecture.NVIDIA_API_KEY.strip():
            return
        self._nvidia_suggested_this_session = True
        import first_run_setup
        first_run_setup.prompt_for_missing_key(
            self.root, PALETTE, "nvidia", on_done=self._on_nvidia_suggestion_done,
            context="drop_warning",
        )

    def _on_nvidia_suggestion_done(self, saved: bool):
        if saved:
            self._refresh_api_status_label()
            self._log("✓ تم حفظ مفتاح NVIDIA - هيتستخدم تلقائي كـ fallback تالت من دلوقتي.")

    def _process_worker(self, lecture: str, selected_paths: list, explain: bool, mode: str = "lecture"):
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
                # استراحة قصيرة بين مرحلة التفريغ ومرحلة التلخيص - المرحلتين
                # بيشاركوا نفس سقف الـ rate limit عند بعض المزوّدين (زي
                # Groq)، فبنديله فرصة "يتنفس" قبل ما نبدأ نضغط عليه تاني.
                self._log_threadsafe("⏳ استراحة قصيرة قبل مرحلة التلخيص...")
                time.sleep(8)
                # التلخيص بيقف عند حدود الأجزاء المحددة بس - لو فيه نص قديم
                # متراكم من قبل كده لسه مش متلخص (مثلاً من محاولة فشلت)،
                # مش هيتجاب زيادة عن التحديد الحالي من غير قصد.
                end_chars = self._end_chars_for_selection(state, selected_paths)
                completed = process_lecture.summarize_new_part(
                    lecture, full_text, state, mode=mode, end_chars=end_chars,
                )
                if completed:
                    self._log_threadsafe(f"✓ خلص! النوتس محفوظة في: {MARKDOWN_FOLDER / (lecture + '.md')}")
                else:
                    self._log_threadsafe(
                        "⚠ اتحفظ اللي خلص لحد دلوقتي بس مش كل الجزء الجديد - دوس 'فرّغ + لخص' تاني عشان تكمل الباقي."
                    )
                    self.root.after(300, self._maybe_suggest_nvidia_after_drop)
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
    def _start_notes_only(self, mode: str = "lecture"):
        lecture = self.current_lecture
        if not lecture:
            show_warning("تنبيه", "اختار محاضرة/جلسة الأول.")
            return
        if self._processing:
            show_warning("تنبيه", "فيه عملية تفريغ/تلخيص شغالة بالفعل - استنى تخلص الأول.")
            return

        transcript_path = TRANSCRIPT_FOLDER / f"{lecture}.txt"
        if not transcript_path.exists():
            show_info("تنبيه", "مفيش نص متفرغ لسه لهذه المحاضرة (فرّغ الأول).")
            return

        # نلقط الأجزاء المحددة دلوقتي في الليست - هتستخدم بعد كده تحدّد
        # التلخيص يوقف عند حدودها بس، بدل ما ياخد كل النص الجديد المتراكم
        # (اللي ممكن يكون فيه نص أقدم لسه مش متلخص من محاولة سابقة فشلت).
        selected_paths = self._get_selected_paths()

        self._log("بدأ تحويل النص المفرّغ الموجود لنوتس (من غير تفريغ صوت إضافي)...")
        threading.Thread(
            target=self._notes_only_worker, args=(lecture, transcript_path, mode, selected_paths), daemon=True
        ).start()

    def _notes_only_worker(self, lecture: str, transcript_path, mode: str = "lecture", selected_paths: list = None):
        self._begin_processing()
        try:
            state = load_state(lecture)
            with open(transcript_path, "r", encoding="utf-8") as f:
                full_text = f.read()

            if not full_text.strip():
                self._log_threadsafe("النص الخام فاضي.")
                return

            end_chars = self._end_chars_for_selection(state, selected_paths or [])
            if end_chars is not None:
                self._log_threadsafe(
                    f"[i] هيلخّص لحد نهاية الأجزاء المحددة بس ({len(selected_paths)} جزء/أجزاء)، مش كل النص الجديد المتراكم."
                )
            completed = process_lecture.summarize_new_part(
                lecture, full_text, state, mode=mode, end_chars=end_chars,
            )
            if completed:
                self._log_threadsafe(f"✓ خلص! النوتس محفوظة في: {MARKDOWN_FOLDER / (lecture + '.md')}")
            else:
                self._log_threadsafe(
                    "⚠ اتحفظ اللي خلص لحد دلوقتي بس مش كل الجزء الجديد - دوس الزرار تاني عشان تكمل الباقي."
                )
                self.root.after(300, self._maybe_suggest_nvidia_after_drop)
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
        win.configure(bg=PALETTE["bg"])
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        win.geometry("780x620")

        header = tk.Frame(win, bg=PALETTE["danger"], width=780)
        header.pack(fill="x")
        header.pack_propagate(False)
        header.configure(height=64)
        tk.Label(
            header, text="🗑  مسح بيانات المحاضرة", bg=PALETTE["danger"], fg="white",
            font=("Segoe UI", 14, "bold"), anchor="w",
        ).pack(side="left", padx=20, pady=14)

        ttk.Label(
            win, text=f"اختار بالظبط أي ريكورد وأي تفريغ عايز تمسحه من «{lecture}»:",
            background=PALETTE["bg"], foreground=PALETTE["text"],
            font=("Segoe UI", 10, "bold"), justify="right", wraplength=720,
        ).pack(anchor="e", padx=16, pady=(16, 4))

        ttk.Label(
            win,
            # سطرين صريحين (\n) بدل الاعتماد على wraplength التلقائي -
            # حساب عرض الخط بيختلف بين ويندوز ولينكس خصوصًا مع الإيموجي،
            # فسطر صريح مضمون 100% على أي نظام تشغيل.
            text="🎙 = ملف الصوت الخام لهذا الجزء\n"
                 "📝 = التفريغ بتاعه بس (متاح للأجزاء المتفرّغة اللي لسه "
                 "مش متشرّحة فقط، لأن اللي اتشرح بالفعل بُنيت النوتس عليه)",
            background=PALETTE["bg"], foreground=PALETTE["text_muted"],
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
        # الهيدر بيتحط جوه wrapper بعرض ثابت (= مجموع عرض الأعمدة بالظبط)
        # ومربوط بـ side="left" - يعني بداياه دايمًا من x=0 في list_outer.
        # الصفوف تحت (inner جوه الـ canvas) برضه متحطة بـ anchor="nw" يعني
        # بداياها من x=0 في الـ canvas، والـ canvas نفسه بياخد نفس عرض
        # list_outer بالظبط (fill="both", expand=True). فبكده الاتنين
        # (الهيدر وصفوف البيانات) بيبدأوا من نفس نقطة x=0 تمامًا، فالأعمدة
        # بتتصاف فوق بعض صح - بدل الشكل القديم اللي كان بيخلي الهيدر يلزق
        # بحافة يمين list_outer الواسعة (fill="x") بينما الصفوف بتتصاف
        # لحافة يمين الـ inner الأضيق (بعرض مجموع الأعمدة بس)، فبيحصل فرق
        # (drift) بينهم بمقدار الفراغ الغير مستخدم على يسار الـ inner.
        TOTAL_COL_WIDTH = sum(COL_WIDTHS.values())
        header_row = tk.Frame(header, width=TOTAL_COL_WIDTH, height=30, bg=PALETTE["card"])
        header_row.pack_propagate(False)
        header_row.pack(side="left")
        # ترتيب الأعمدة من اليمين لليسار (RTL): اسم الملف، المدة، الحالة،
        # شيك الصوت، شيك التفريغ - بنعبّي من side="right" بنفس الترتيب.
        for key, text in [
            ("filename", "اسم الملف"), ("duration", "المدة/الحجم"),
            ("status", "الحالة"), ("audio_cb", "🎙"), ("tr_cb", "📝"),
        ]:
            cell = _make_cell(header_row, COL_WIDTHS[key], PALETTE["card"])
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
            background=PALETTE["bg"], foreground=PALETTE["danger"],
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
        win.configure(bg=PALETTE["bg"])
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        win.geometry("440x300")

        header = tk.Frame(win, bg=PALETTE["danger"], width=440)
        header.pack(fill="x")
        header.pack_propagate(False)
        header.configure(height=56)
        tk.Label(
            header, text="⚠  تأكيد نهائي", bg=PALETTE["danger"], fg="white",
            font=("Segoe UI", 13, "bold"), anchor="w",
        ).pack(side="left", padx=20, pady=12)

        ttk.Label(
            win, text=message, background=PALETTE["bg"], foreground=PALETTE["danger"],
            font=("Segoe UI", 10, "bold"), justify="right", wraplength=400,
        ).pack(anchor="e", padx=16, pady=(16, 10))

        ttk.Label(
            win, text="اكتب اسم المحاضرة بالظبط عشان تأكد:",
            background=PALETTE["bg"], foreground=PALETTE["text"],
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
            row_name, text=f"«{lecture}»", background=PALETTE["bg"],
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

    def _show_transcript_viewer(self):
        lecture = self.current_lecture
        txt_path = TRANSCRIPT_FOLDER / f"{lecture}.txt" if lecture else None
        if not lecture or not txt_path.exists():
            show_info("تنبيه", "مفيش ملف تفريغ لسه لهذه المحاضرة.")
            return

        with open(txt_path, "r", encoding="utf-8") as f:
            content = f.read()

        win = tk.Toplevel(self.root)
        win.title(f"تفريغ: {lecture}")
        win.geometry("820x680")
        win.configure(bg=PALETTE["bg"])

        header = tk.Frame(win, bg=PALETTE["accent"], width=820)
        header.pack(fill="x")
        header.pack_propagate(False)
        header.configure(height=52)
        tk.Label(
            header, text=f"📃  تفريغ: {lecture}", bg=PALETTE["accent"], fg="white",
            font=("Segoe UI", 12, "bold"), anchor="w",
        ).pack(side="left", padx=20, pady=10)

        toolbar = ttk.Frame(win, style="TFrame")
        toolbar.pack(fill="x", padx=8, pady=(6, 0))

        copy_btn = self._card_button(
            toolbar, "📋  Copy", None, PALETTE["accent_soft"], PALETTE["accent_soft_dark"],
            font=("Segoe UI", 9, "bold"), padx=12, pady=6,
        )
        copy_btn.pack(side="left")

        def _do_copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self.root.update()
            # فيدباك بصري فوري: يتحول أخضر ومكتوب "Copied" لمدة ثانيتين،
            # بعدها يرجع لشكله وألوانه الأصلية تلقائي.
            copy_btn.config(
                text="✓  Copied", bg=PALETTE["success"], activebackground=PALETTE["success"],
            )
            copy_btn._normal_bg = PALETTE["success"]

            def _reset():
                copy_btn.config(
                    text="📋  Copy", bg=PALETTE["accent_soft"],
                    activebackground=PALETTE["accent_soft_dark"],
                )
                copy_btn._normal_bg = PALETTE["accent_soft"]

            win.after(2000, _reset)

        copy_btn.config(command=_do_copy)
        _add_tooltip(copy_btn, "Copy the entire raw transcript text to the clipboard.")

        text_frame = tk.Frame(win, bg=PALETTE["bg"])
        text_frame.pack(fill="both", expand=True, padx=8, pady=8)

        text_widget = scrolledtext.ScrolledText(
            text_frame, wrap="word", font=("Segoe UI", 11),
            bg="white", fg=PALETTE["text"], relief="flat", bd=1,
            padx=12, pady=10,
        )
        text_widget.pack(fill="both", expand=True)
        text_widget.insert("1.0", content)
        text_widget.config(state="disabled")  # عرض للقراءة بس، من غير تعديل بالغلط

    def _show_notes_viewer(self):
        lecture = self.current_lecture
        md_path = MARKDOWN_FOLDER / f"{lecture}.md" if lecture else None
        if not lecture or not md_path.exists():
            show_info("تنبيه", "مفيش ملف نوتس لسه لهذه المحاضرة.")
            return

        win = tk.Toplevel(self.root)
        win.title(f"نوتس: {lecture}")
        win.geometry("820x680")
        win.configure(bg=PALETTE["bg"])

        header = tk.Frame(win, bg=PALETTE["accent"], width=820)
        header.pack(fill="x")
        header.pack_propagate(False)
        header.configure(height=52)
        tk.Label(
            header, text=f"📄  نوتس: {lecture}", bg=PALETTE["accent"], fg="white",
            font=("Segoe UI", 12, "bold"), anchor="w",
        ).pack(side="left", padx=20, pady=10)

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
        body = md_lib.markdown(
            content_with_math,
            extensions=["fenced_code", "codehilite", "tables", "nl2br"],
            extension_configs={
                "codehilite": {"guess_lang": False, "css_class": "codehilite", "noclasses": False},
            },
        )
        body = _colorize_blockquotes(body)

        highlight_css = "\n".join(
            f'blockquote.{cls} {{ background: {bg}; border-right: 4px solid {border}; }}'
            for cls, bg, border in HIGHLIGHT_STYLES.values()
        )

        # تلوين الكود (syntax highlighting) حقيقي عن طريق Pygments بستايل
        # غامق قريب من VS Code Dark، بدل الخلفية الرمادية البسيطة القديمة.
        pygments_css = HtmlFormatter(style="monokai").get_style_defs(".codehilite")

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
code {{ direction: ltr; text-align: left; unicode-bidi: embed; font-family: Consolas, 'Courier New', monospace;
    background: #f4f4f4; border-radius: 4px; padding: 2px 5px; }}
div.codehilite {{ direction: ltr; text-align: left; margin: 12px 0; border-radius: 8px;
    overflow: hidden; background: #1e1e1e; box-shadow: 0 1px 4px rgba(0,0,0,0.25); }}
div.codehilite pre {{ direction: ltr; text-align: left; unicode-bidi: embed;
    font-family: Consolas, 'Courier New', monospace; font-size: 13px;
    margin: 0; padding: 14px 16px; overflow-x: auto; background: #1e1e1e; color: #d4d4d4; }}
div.codehilite code {{ background: transparent; padding: 0; }}
blockquote {{ direction: rtl; text-align: right; background: #f8f4e3; border-right: 4px solid #d4a017;
    margin: 10px 0; padding: 8px 14px; border-radius: 4px; }}
{highlight_css}
{pygments_css}
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
            # مهم: كل الثريدز daemon، يعني لو قفلنا النافذة على طول من غير
            # ما نستنى، ثريد الكتابة/الضغط بيتقتل فجأة نص الشغل وممكن يبوظ
            # آخر جزء صوتي. بدل ما نقفل على طول، بنستنى الثريد يخلص فعلياً
            # (يقفل الملف ويضغطه) قبل ما نعمل destroy.
            self.record_btn.config(state="disabled")
            self.status_label.config(text="⏳ بيقفل ويحفظ آخر جزء قبل الإغلاق...", foreground=PALETTE["warning"])
            self._log("جاري حفظ آخر جزء تسجيل قبل إغلاق البرنامج، من فضلك استنى...")
            self._wait_write_thread_then_close()
            return
        elif self._processing:
            if not ask_yesno(
                "تنبيه",
                "فيه عملية تفريغ/تلخيص شغالة دلوقتي. لو قفلت البرنامج دلوقتي "
                "ممكن يضيع جزء من الشغل الحالي.\n\nعايز تقفل فعلاً؟",
            ):
                return
        self.root.destroy()

    def _wait_write_thread_then_close(self, waited_ms: int = 0):
        """بيستنى ثريد الكتابة (اللي بيقفل الملف الحالي ويضغطه) يخلص فعلياً
        قبل إغلاق النافذة، بدل قفل فوري ممكن يقطع الكتابة نص الطريق.
        فيه حد أقصى للانتظار (30 ثانية) كـ fallback احتياطي بس، عشان
        البرنامج ميفضلش عالق لو حصل عطل غريب في الثريد نفسه."""
        thread_done = self._write_thread is None or not self._write_thread.is_alive()
        if thread_done or waited_ms >= 30000:
            self.root.destroy()
            return
        self.root.after(200, self._wait_write_thread_then_close, waited_ms + 200)


def main():
    root = tk.Tk()
    app = StudyApp(root)
    root.protocol("WM_DELETE_WINDOW", app._on_close)
    root.mainloop()


if __name__ == "__main__":
    main()