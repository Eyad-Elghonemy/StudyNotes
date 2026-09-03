"""
نافذة إعداد أول تشغيل: بتظهر لو مفيش أي API key متسجل خالص (لا Gemini ولا
Groq)، بتاخد المفتاح/المفاتيح من المستخدم، تتحقق منهم فعليًا بطلب تجريبي
خفيف (مش مجرد تأكد إنهم مش فاضيين)، تحفظهم في .env، وتحدّث الثوابت في
process_lecture مباشرة عشان البرنامج يكمل شغل عادي من غير ما يحتاج
restart يدوي.

الاستخدام (من gui_app.py):
    import first_run_setup
    if not first_run_setup.keys_configured():
        first_run_setup.show_dialog(root, palette=PALETTE, on_done=callback)
"""

import os
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk

import process_lecture

PROJECT_DIR = Path(__file__).resolve().parent


def _center_window(win: tk.Toplevel, root: tk.Tk, max_height_ratio: float = 1.0) -> None:
    """
    بيوسّط نافذة فوق النافذة الرئيسية، لكن بيتأكد كمان إنها تفضل بالكامل
    جوه حدود الشاشة (مش بس جوه حدود النافذة الرئيسية) - عشان لو النافذة
    الرئيسية قريبة من حافة الشاشة، نافذة الحوار الفرعية متطلعش جزء منها
    برّه الشاشة (زي زرار Save اللي كان بيختفي تحت الحافة).

    مهم: بنستخدم winfo_reqwidth/reqheight (الحجم *المطلوب* من مدير
    التخطيط) مش winfo_width/height (الحجم الفعلي المرسوم على الشاشة) -
    لأن التاني بيرجع قيم غير دقيقة (غالبًا أصغر من المطلوب فعليًا، خصوصًا
    مع نصوص متعددة الأسطر زي wraplength) لو النافذة لسه ما اترسمتش على
    الشاشة فعليًا وقت الحساب، وده كان بيخلي النافذة تتحسب أصغر من محتواها
    الحقيقي فتتقص أي حاجة تحت (زي صف الأزرار) برّه حدودها المرسومة.
    كمان بنضيف ارتفاع/عرض هامش أمان صغير (safety margin) احتياطي.

    max_height_ratio: لو المحتوى الحقيقي أطول من نسبة معيّنة من ارتفاع
    الشاشة (افتراضيًا 100%، يعني بلا حد)، بيتقصّ الارتفاع عند الحد ده -
    مفيد للنوافذ اللي عندها منطقة قابلة للـ Scroll جواها (زي كارتات
    كتير) عشان النافذة تفضل بحجم معقول حتى لو المحتوى طويل جدًا.
    """
    win.update_idletasks()
    w = win.winfo_reqwidth() + 4
    h = win.winfo_reqheight() + 4
    max_h = int(win.winfo_screenheight() * max_height_ratio)
    if h > max_h:
        h = max_h
    # نطبّق الحجم صراحة (مش بس الموضع) عشان نضمن إن مدير النوافذ (WM)
    # هيحترم الحجم ده بالظبط، بدل ما يعتمد على حجم "تلقائي" ممكن يجيله
    # أصغر لو اترسم في لحظة مختلفة.
    x = root.winfo_x() + (root.winfo_width() - w) // 2
    y = root.winfo_y() + (root.winfo_height() - h) // 2

    screen_w, screen_h = win.winfo_screenwidth(), win.winfo_screenheight()
    x = max(0, min(x, screen_w - w))
    y = max(0, min(y, screen_h - h))
    win.geometry(f"{w}x{h}+{x}+{y}")


ENV_PATH = PROJECT_DIR / ".env"
ENV_EXAMPLE_PATH = PROJECT_DIR / ".env.example"


def _fix_entry_keyboard_shortcuts(entry: ttk.Entry) -> None:
    """
    Ctrl+V/C/X/A بيعتمدوا افتراضيًا في tkinter على الـ keysym اللي بيرجعه
    نظام التشغيل، واللي بيتغيّر حسب لغة الكيبورد الحالية (عربي مثلاً) -
    فلو المستخدم كان محول عربي وقت الضغط، Ctrl+V ممكن ميشتغلش خالص. الحل:
    نربط على event.keycode (رقم المفتاح الفيزيائي الثابت) بدل الرمز اللي
    بيتغيّر مع اللغة، فيشتغل صح بغض النظر عن لغة الكيبورد المفعّلة.

    ملحوظة مهمة: مينفعش نحط الرقم نفسه جوه اسم الحدث زي
    "<Control-Key-86>" - دي مش صيغة صحيحة في Tk (بيتوقع اسم/رمز مفتاح،
    مش رقم keycode خام) وبترمي TclError: "bad event type or keysym"
    فورًا وقت الـ bind، اللي كان بيوقف تنفيذ باقي بناء النافذة (زي صف
    الأزرار تحت) في نص الطريق - وده السبب الحقيقي وراء النافذة اللي كانت
    بتظهر "مقصوصة". الحل الصح: نعمل bind عام على <Control-KeyPress>
    ونفلتر جوه الـ handler نفسه على أساس event.keycode.
    """
    # keycode 86 = V, 67 = C, 88 = X, 65 = A على كل الأنظمة تقريبًا (نفس
    # ترتيب لوحة المفاتيح الفيزيائي مش الرمز المطبوع)
    def _paste(event=None):
        try:
            entry.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        entry.insert("insert", entry.clipboard_get())
        return "break"

    def _copy(event=None):
        try:
            entry.clipboard_clear()
            entry.clipboard_append(entry.selection_get())
        except tk.TclError:
            pass
        return "break"

    def _cut(event=None):
        _copy(event)
        try:
            entry.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        return "break"

    def _select_all(event=None):
        entry.selection_range(0, "end")
        return "break"

    _KEYCODE_HANDLERS = {86: _paste, 67: _copy, 88: _cut, 65: _select_all}

    def _on_ctrl_keypress(event):
        handler = _KEYCODE_HANDLERS.get(event.keycode)
        if handler is not None:
            return handler(event)

    entry.bind("<Control-KeyPress>", _on_ctrl_keypress)
    # نفس الأزرار بأحرف إنجليزي/عربي عادي برضو (احتياطي لو الـ keycode
    # مختلف على بعض الأنظمة)
    entry.bind("<<Paste>>", _paste)
    entry.bind("<<Copy>>", _copy)
    entry.bind("<<Cut>>", _cut)

    # الحل المضمون 100% بغض النظر عن لغة الكيبورد: قايمة كليك يمين، لأنها
    # مش بتعتمد على أي مفتاح أو رمز لوحة مفاتيح خالص - بس على الماوس.
    menu = tk.Menu(entry, tearoff=0)
    menu.add_command(label="Cut", command=lambda: _cut(None))
    menu.add_command(label="Copy", command=lambda: _copy(None))
    menu.add_command(label="Paste", command=lambda: _paste(None))
    menu.add_separator()
    menu.add_command(label="Select All", command=lambda: _select_all(None))

    def _show_context_menu(event):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    entry.bind("<Button-3>", _show_context_menu)  # كليك يمين (زرار الماوس التاني)


def keys_configured() -> bool:
    """اتسجّل الاتنين الأساسيين (Gemini وGroq) فعليًا في الـ environment
    الحالية؟ (بعد load_dotenv() اللي بتتنفذ أول ما البرنامج يفتح). الاتنين
    مطلوبين دلوقتي (مش واحد بس) عشان الـ fallback بين المزوّدين يشتغل صح
    من أول استخدام. NVIDIA فضل اختياري - وجوده أو غيابه ملوش تأثير هنا."""
    return bool(os.environ.get("GEMINI_API_KEY", "").strip()) and bool(
        os.environ.get("GROQ_API_KEY", "").strip()
    )


def ensure_env_file_exists() -> None:
    """لو .env مش موجود، بيتعمل تلقائيًا بناءً على .env.example (نفس
    القالب/التعليقات، بس المفاتيح فاضية لحد ما المستخدم يملاها من النافذة)."""
    if ENV_PATH.exists():
        return
    try:
        if ENV_EXAMPLE_PATH.exists():
            content = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        else:
            content = "GEMINI_API_KEY=\nGROQ_API_KEY=\n"
        ENV_PATH.write_text(content, encoding="utf-8")
    except Exception:
        pass  # لو فشل الإنشاء (صلاحيات كتابة مثلاً)، النافذة هتوضح المشكلة بعدين لو حصل خطأ حفظ


def _test_gemini_key(key: str) -> tuple[bool, str]:
    """طلب تجريبي خفيف جدًا (قائمة الموديلات المتاحة، من غير توليد محتوى
    فعلي) عشان نتأكد إن المفتاح مقبول من السيرفر فعلاً."""
    try:
        from google import genai
        client = genai.Client(api_key=key)
        next(iter(client.models.list()), None)
        return True, ""
    except Exception as e:
        return False, process_lecture.friendly_error(e)


def _test_groq_key(key: str) -> tuple[bool, str]:
    try:
        from groq import Groq
        client = Groq(api_key=key)
        client.models.list()
        return True, ""
    except Exception as e:
        return False, process_lecture.friendly_error(e)


def _test_nvidia_key(key: str) -> tuple[bool, str]:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key, base_url="https://integrate.api.nvidia.com/v1")
        # نفس فكرة الفحص الخفيف بتاع Gemini/Groq - رد بسيط جدًا كفاية
        # للتأكد إن المفتاح مقبول من غير استهلاك quota حقيقي.
        client.chat.completions.create(
            model=process_lecture.NVIDIA_TEXT_MODEL,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )
        return True, ""
    except Exception as e:
        return False, process_lecture.friendly_error(e)


def _save_env(gemini_key: str, groq_key: str, nvidia_key: str = "") -> None:
    """يحدّث GEMINI_API_KEY/GROQ_API_KEY/NVIDIA_API_KEY في .env من غير ما
    يلمس أي سطر تاني موجود فيه (زي STUDYNOTES_DIR)."""
    lines = []
    if ENV_PATH.exists():
        try:
            lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []

    def _set_line(key_name: str, value: str):
        prefix = f"{key_name}="
        for i, line in enumerate(lines):
            if line.strip().startswith(prefix):
                lines[i] = f"{key_name}={value}"
                return
        lines.append(f"{key_name}={value}")

    _set_line("GEMINI_API_KEY", gemini_key)
    _set_line("GROQ_API_KEY", groq_key)
    if nvidia_key:
        _set_line("NVIDIA_API_KEY", nvidia_key)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # نحدّث الـ environment الحالية وثوابت process_lecture مباشرة، لأنها
    # كانت اتقرت مرة واحدة بس وقت الـ import - من غير التحديث ده، البرنامج
    # هيفضل مش شايف المفاتيح الجديدة لحد ما يتعمله restart يدوي.
    os.environ["GEMINI_API_KEY"] = gemini_key
    os.environ["GROQ_API_KEY"] = groq_key
    process_lecture.GEMINI_API_KEY = gemini_key
    process_lecture.GROQ_API_KEY = groq_key
    if nvidia_key:
        os.environ["NVIDIA_API_KEY"] = nvidia_key
        process_lecture.NVIDIA_API_KEY = nvidia_key


LOGOS_DIR = PROJECT_DIR / "assets" / "logos"

# كاش لصور اللوجوهات بعد التحميل والتحجيم، مفتاحها (اسم_الملف, الحجم) -
# لازم نحتفظ بمرجع Python للـ PhotoImage/PIL ImageTk عشان الصورة متتمسحش
# من الذاكرة (garbage collected) وتختفي من على الشاشة وهي لسه معروضة.
_logo_cache: dict = {}


def _load_logo(filename: str, size: int):
    """بتحمّل لوجو من assets/logos وتحجّمه لمربع (size x size) بأعلى جودة
    ممكنة (Pillow LANCZOS) مع الحفاظ على الشفافية. بترجع None لو Pillow
    مش متاحة أو الملف مش موجود - عشان أي مكان بيستخدمها يقدر يرجع للإيموجي
    القديم كـ fallback بدل ما يعمل crash."""
    cache_key = (filename, size)
    if cache_key in _logo_cache:
        return _logo_cache[cache_key]
    path = LOGOS_DIR / filename
    if not path.exists():
        return None
    try:
        from PIL import Image, ImageTk
        img = Image.open(path).convert("RGBA")
        img.thumbnail((size, size), Image.LANCZOS)
        photo = ImageTk.PhotoImage(img)
    except Exception:
        return None
    _logo_cache[cache_key] = photo
    return photo


# معلومات كل مزوّد لازمة لنافذة "محتاج مفتاح" (اسم متغير الـ .env، رابط
# الحصول على مفتاح، ودالة الفحص الفعلي، ملف اللوجو، ولون البراند الرسمي
# بتاعه). مركزية هنا عشان أي مزوّد جديد يتضاف بسهولة في مكان واحد بس.
PROVIDER_INFO = {
    "gemini": {
        "display": "Gemini", "env_key": "GEMINI_API_KEY",
        "url": "https://ai.google.dev/", "test_fn": _test_gemini_key,
        "logo": "gemini.png", "brand_color": "#4285F4",
        "steps": [
            "Open the link and sign in with your Google account.",
            "Click \"Get API key\" → \"Create API key\".",
            "Copy the key and paste it below.",
        ],
    },
    "groq": {
        "display": "Groq", "env_key": "GROQ_API_KEY",
        "url": "https://console.groq.com/", "test_fn": _test_groq_key,
        "logo": "groq.png", "brand_color": "#F55036",
        "steps": [
            "Open the link and sign up (free, no card required).",
            "Go to \"API Keys\" in the left menu → \"Create API Key\".",
            "Copy the key and paste it below.",
        ],
    },
    "nvidia": {
        "display": "NVIDIA", "env_key": "NVIDIA_API_KEY",
        "url": "https://build.nvidia.com/", "test_fn": _test_nvidia_key,
        "logo": "nvidia.png", "brand_color": "#76B900",
        "steps": [
            "Open the link and sign in (free developer account, no card required).",
            "Open any model card and click \"Get API Key\". The key starts with nvapi-.",
            "Copy the key and paste it below.",
        ],
    },
}


class _MissingKeyDialog:
    """نافذة صغيرة بتظهر لما المستخدم يختار موديل يدويًا (مش Auto) لمزوّد
    مفيش مفتاح API متسجل له - بتوجهه بالظبط لمنين يجيب المفتاح، وتتحقق
    منه وتحفظه فورًا (نفس أسلوب _SetupDialog بالظبط لكن لمفتاح واحد بس)."""

    def __init__(self, root: tk.Tk, palette: dict, provider: str, on_done, context: str = "model_select"):
        info = PROVIDER_INFO[provider]
        self.p = palette
        self.provider = provider
        self.info = info
        self.on_done = on_done  # on_done(saved: bool)

        self.win = tk.Toplevel(root)
        self.win.title(f"Get a {info['display']} API Key")
        self.win.configure(bg=palette["bg"])
        self.win.transient(root)
        self.win.protocol("WM_DELETE_WINDOW", self._on_cancel)

        WIDTH = 440
        # الهيدر بقى بلون البراند الرسمي بتاع المزوّد نفسه (أخضر NVIDIA،
        # برتقالي/أحمر Groq، أزرق Gemini) بدل لون عام واحد لكل المزودين -
        # عشان يبقى واضح بصريًا إنت بتضيف مفتاح لمين بالظبط. في حالة
        # "drop_warning" (اقتراح إضافة مزوّد كـ fallback بعد توقف جزئي)
        # بنفضل مستخدمين لون البراند برضو (علشان اللوجو يفضل واضح ومتناسق
        # معاه) وبس بنضيف أيقونة تحذير ⚠️ جنب الاسم بدل الاعتماد على لون
        # التحذير العام.
        header_color = info.get("brand_color", palette["accent"])
        header = tk.Frame(self.win, bg=header_color, width=WIDTH)
        header.pack(fill="x")
        header.pack_propagate(False)
        header.configure(height=56)

        logo_photo = _load_logo(info.get("logo", ""), size=30)
        if logo_photo is not None:
            # دايرة بيضاء صغيرة خلف اللوجو - عشان تضمن تباين واضح حتى لو
            # لون اللوجو نفسه قريب من لون الهيدر (زي NVIDIA الأخضر فوق
            # هيدر أخضر، أو Groq الأحمر فوق هيدر أحمر/برتقالي).
            badge_size = 40
            badge = tk.Canvas(
                header, width=badge_size, height=badge_size, bg=header_color,
                highlightthickness=0, bd=0,
            )
            badge.pack(side="left", padx=(16, 10), pady=8)
            badge.create_oval(1, 1, badge_size - 1, badge_size - 1, fill="white", outline="")
            badge.create_image(badge_size // 2, badge_size // 2, image=logo_photo)
            badge.image = logo_photo  # مرجع إضافي يمنع الـ garbage collection
        else:
            header_icon = "⚠️" if context == "drop_warning" else "🔑"
            tk.Label(
                header, text=header_icon, bg=header_color, fg="white",
                font=("Segoe UI", 16), anchor="w",
            ).pack(side="left", padx=(20, 6))

        title_text = f"{info['display']} Key Needed"
        if context == "drop_warning":
            title_text = f"⚠ {title_text}"
        tk.Label(
            header, text=title_text, bg=header_color, fg="white",
            font=("Segoe UI", 13, "bold"), anchor="w",
        ).pack(side="left", pady=13)

        body = tk.Frame(self.win, bg=palette["bg"])
        body.pack(fill="both", padx=20, pady=(14, 8))

        if context == "drop_warning":
            message = (
                "A recent summarization run stopped partway through because both "
                "Gemini and Groq hit their rate limits at the same time. Adding a "
                f"{info['display']} key gives you a 3rd fallback so this doesn't "
                "happen again on long sessions."
            )
        else:
            message = (
                f"The model you selected needs a {info['display']} API key, which "
                "isn't configured yet."
            )

        tk.Label(
            body, text=message, bg=palette["bg"], fg=palette["text"], font=("Segoe UI", 9),
            justify="left", wraplength=WIDTH - 40,
        ).pack(anchor="w", pady=(0, 10))

        link_btn = tk.Button(
            body, text=f"🔗 Get your free {info['display']} key",
            command=lambda: webbrowser.open(info["url"]),
            font=("Segoe UI", 9, "bold"), bg=palette["accent"], fg="white",
            activebackground=palette["accent_dark"], activeforeground="white",
            relief="flat", bd=0, cursor="hand2", padx=10, pady=6,
        )
        link_btn.pack(anchor="w", fill="x", pady=(0, 8))

        steps_frame = tk.Frame(body, bg=palette["bg"])
        steps_frame.pack(anchor="w", fill="x", pady=(0, 10))
        for i, step in enumerate(info.get("steps", []), 1):
            tk.Label(
                steps_frame, text=f"{i}.  {step}", bg=palette["bg"], fg=palette["text_muted"],
                font=("Segoe UI", 8), justify="left", wraplength=WIDTH - 40, anchor="w",
            ).pack(anchor="w", pady=1)

        self.key_var = tk.StringVar()
        entry = ttk.Entry(body, textvariable=self.key_var, width=44, justify="left", show="•")
        entry.pack(anchor="w", fill="x", pady=(0, 4))
        _fix_entry_keyboard_shortcuts(entry)

        self.status_label = tk.Label(
            body, text="", bg=palette["bg"], font=("Segoe UI", 8, "bold"),
            justify="left", wraplength=WIDTH - 40,
        )
        self.status_label.pack(anchor="w", pady=(0, 4))

        btn_row = tk.Frame(body, bg=palette["bg"])
        btn_row.pack(fill="x", pady=(8, 12))

        cancel_text = "Maybe later" if context == "drop_warning" else "Use Auto instead"
        cancel_btn = tk.Button(
            btn_row, text=cancel_text, command=self._on_cancel,
            font=("Segoe UI", 9), bg=palette["bg"], fg=palette["text_muted"],
            relief="flat", bd=0, cursor="hand2", padx=8, pady=6,
        )
        cancel_btn.pack(side="left")

        self.save_btn = tk.Button(
            btn_row, text="💾 Save", command=self._do_validate_and_save,
            font=("Segoe UI", 10, "bold"), bg=palette["accent"], fg="white",
            activebackground=palette["accent_dark"], activeforeground="white",
            relief="flat", bd=0, cursor="hand2", padx=14, pady=6,
        )
        self.save_btn.pack(side="right")

        _center_window(self.win, root)
        self.win.resizable(False, False)
        self.win.grab_set()
        entry.focus_set()

    def _do_validate_and_save(self):
        key_val = self.key_var.get().strip()
        if not key_val:
            self.status_label.config(text="⚠ Enter a key first.", foreground=self.p["danger"])
            return
        self.status_label.config(text="")
        self.save_btn.config(state="disabled", text="⏳ Checking key...")
        threading.Thread(target=self._validate_worker, args=(key_val,), daemon=True).start()

    def _validate_worker(self, key_val: str):
        ok, err = self.info["test_fn"](key_val)
        self.win.after(0, self._on_validated, key_val, ok, err)

    def _on_validated(self, key_val, ok, err):
        self.save_btn.config(state="normal", text="💾 Save")
        if not ok:
            self.status_label.config(text=f"✗ Invalid: {err}", foreground=self.p["danger"])
            return

        try:
            kwargs = {"gemini_key": process_lecture.GEMINI_API_KEY, "groq_key": process_lecture.GROQ_API_KEY,
                      "nvidia_key": process_lecture.NVIDIA_API_KEY}
            kwargs[{"gemini": "gemini_key", "groq": "groq_key", "nvidia": "nvidia_key"}[self.provider]] = key_val
            _save_env(**kwargs)
        except Exception as e:
            self.status_label.config(text=f"⚠ Save failed: {e}", foreground=self.p["danger"])
            return

        self.win.grab_release()
        self.win.destroy()
        if self.on_done:
            self.on_done(True)

    def _on_cancel(self):
        self.win.grab_release()
        self.win.destroy()
        if self.on_done:
            self.on_done(False)


def prompt_for_missing_key(root: tk.Tk, palette: dict, provider: str, on_done=None, context: str = "model_select"):
    """بتفتح نافذة توجيه لمفتاح مزوّد معيّن (provider: 'gemini'/'groq'/
    'nvidia'). context: 'model_select' (المستخدم اختار موديل يدوي
    محتاج مفتاح ناقص) أو 'drop_warning' (حصل drop فعلي وإحنا بننصح
    بإضافة fallback تالت). on_done(saved: bool) بيتنادى لما المستخدم
    يحفظ مفتاح صالح (True) أو يلغي/يقفل النافذة (False). بترجع الـ
    dialog نفسه عشان المستدعي يقدر يعمل root.wait_window(dialog.win)
    لو عايز ينتظرها تتقفل قبل ما يكمل (استخدام modal-style)."""
    if provider not in PROVIDER_INFO:
        return None
    return _MissingKeyDialog(root, palette, provider, on_done, context=context)


class _SetupDialog:
    """First-run setup window: both Gemini AND Groq are required (the app
    relies on switching between them automatically), NVIDIA is optional
    but strongly recommended as a 3rd fallback to avoid summarization
    drops on long sessions."""

    def __init__(self, root: tk.Tk, palette: dict, on_done):
        self.root = root
        self.p = palette
        self.on_done = on_done

        WIDTH = 480
        self.win = tk.Toplevel(root)
        self.win.title("Welcome to StudyNotes")
        self.win.configure(bg=palette["bg"])
        self.win.resizable(False, False)
        self.win.transient(root)
        self.win.grab_set()
        self.win.protocol("WM_DELETE_WINDOW", self._on_cancel)

        header = tk.Frame(self.win, bg=palette["accent"], width=WIDTH)
        header.pack(fill="x")
        header.pack_propagate(False)
        header.configure(height=64)
        tk.Label(
            header, text="🔑  First-Time Setup", bg=palette["accent"], fg="white",
            font=("Segoe UI", 14, "bold"), anchor="w",
        ).pack(side="left", padx=20, pady=14)

        # منطقة قابلة للـ Scroll للمحتوى الطويل (3 كروت) - الهيدر وزرار
        # الحفظ برّه الـ scroll وثابتين دايمًا، عشان زرار "Save" يفضل
        # ظاهر مهما كان طول المحتوى وحجم الشاشة (باج قديم كان بيخلي
        # النافذة تطلع أطول من الشاشة والزرار يختفي تمامًا).
        scroll_outer = tk.Frame(self.win, bg=palette["bg"])
        scroll_outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(scroll_outer, bg=palette["bg"], highlightthickness=0, width=WIDTH)
        vsb = ttk.Scrollbar(scroll_outer, orient="vertical", command=canvas.yview)
        body = tk.Frame(canvas, bg=palette["bg"])
        canvas.create_window((0, 0), window=body, anchor="nw", width=WIDTH)
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        # الـ scrollbar بيبان بس لو المحتوى فعلاً أطول من المساحة المتاحة
        # (بيتحدد لاحقًا في _center_window بعد ما نعرف الحجم الحقيقي)
        self._scroll_vsb = vsb
        self._scroll_canvas = canvas

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # padding داخلي حوالين المحتوى (body نفسه بره منطقة الـ canvas
        # مباشرة، فبنحط فريم تاني جواه بالـ padding بدل ما نحط padding
        # على body اللي متحط بـ create_window مش pack)
        body_padded = tk.Frame(body, bg=palette["bg"])
        body_padded.pack(fill="both", expand=True, padx=20, pady=(16, 8))
        body = body_padded  # باقي الكود تحت بيستخدم "body" زي ما هو من غير أي تغيير

        tk.Label(
            body,
            text="Both keys below are required - the app automatically switches "
                 "between them if one fails or times out. Both are free and take "
                 "about a minute to get.",
            bg=palette["bg"], fg=palette["text_muted"], font=("Segoe UI", 9),
            justify="left", wraplength=WIDTH - 40,
        ).pack(anchor="w", pady=(0, 14))

        self.gemini_var = tk.StringVar()
        self.groq_var = tk.StringVar()
        self.nvidia_var = tk.StringVar()

        self.gemini_status = self._key_card(
            body, "gemini", "Gemini API Key", "Required",
            self.gemini_var, PROVIDER_INFO["gemini"]["url"],
            steps=PROVIDER_INFO["gemini"]["steps"],
        )
        self.groq_status = self._key_card(
            body, "groq", "Groq API Key", "Required",
            self.groq_var, PROVIDER_INFO["groq"]["url"],
            steps=PROVIDER_INFO["groq"]["steps"],
        )
        self.nvidia_status = self._key_card(
            body, "nvidia", "NVIDIA API Key", "Optional - recommended",
            self.nvidia_var, PROVIDER_INFO["nvidia"]["url"],
            note="A 3rd fallback provider. Without it, if Gemini and Groq both "
                 "hit a rate limit at the same time on a long session, "
                 "summarization can stop partway through. Free forever, no "
                 "credit card required. You can always add this later from "
                 "⚙ Model Settings.",
            steps=PROVIDER_INFO["nvidia"]["steps"],
        )

        self.info_label = tk.Label(
            self.win, text="", bg=palette["bg"], fg=palette["danger"],
            font=("Segoe UI", 9, "bold"), justify="left", wraplength=WIDTH - 40,
        )
        self.info_label.pack(anchor="w", padx=20, pady=(4, 4))

        self.save_btn = self._card_button(self.win, "💾  Save and Continue", self._do_validate_and_save)
        self.save_btn.pack(fill="x", padx=20, pady=(0, 18))

        _center_window(self.win, root, max_height_ratio=0.85)
        # لو المحتوى أقصر من المساحة المتاحة، الـ scrollbar مالوش لازمة -
        # نخفيه عشان مايبانش شريط فاضي مالوش استخدام
        self.win.update_idletasks()
        if body.winfo_reqheight() <= canvas.winfo_height():
            vsb.pack_forget()

    def _card_button(self, parent, text, command):
        btn = tk.Button(
            parent, text=text, command=command,
            font=("Segoe UI", 10, "bold"), bg=self.p["accent"], fg="white",
            activebackground=self.p["accent_dark"], activeforeground="white",
            relief="flat", bd=0, cursor="hand2", padx=14, pady=10,
        )
        return btn

    def _key_card(self, parent, provider, title, badge_text, var, url, note=None, steps=None):
        # accent_color بقى بيتاخد تلقائي من لون البراند الرسمي بتاع
        # المزوّد (PROVIDER_INFO) بدل ما يتحدد يدويًا لكل كارت - عشان
        # الشريط الجانبي ولون اللوجو يفضلوا متسقين مع باقي النوافذ.
        info = PROVIDER_INFO[provider]
        accent_color = info.get("brand_color", self.p["accent"])

        card = tk.Frame(
            parent, bg=self.p["card"], highlightthickness=1,
            highlightbackground=self.p["border"], highlightcolor=self.p["border"],
        )
        card.pack(fill="x", pady=(0, 10))

        strip = tk.Frame(card, bg=accent_color, width=5)
        strip.pack(side="left", fill="y")

        inner = tk.Frame(card, bg=self.p["card"])
        inner.pack(side="left", fill="both", expand=True, padx=16, pady=12)

        title_row = tk.Frame(inner, bg=self.p["card"])
        title_row.pack(fill="x", anchor="w")

        logo_photo = _load_logo(info.get("logo", ""), size=20)
        if logo_photo is not None:
            logo_label = tk.Label(title_row, image=logo_photo, bg=self.p["card"])
            logo_label.image = logo_photo  # مرجع يمنع الـ garbage collection
            logo_label.pack(side="left")
        tk.Label(
            title_row, text=title, bg=self.p["card"], fg=self.p["text"],
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left", padx=(8, 8))
        tk.Label(
            title_row, text=badge_text, bg=self.p["card"], fg=self.p["text_muted"],
            font=("Segoe UI", 8, "italic"),
        ).pack(side="left")

        if note:
            tk.Label(
                inner, text=note, bg=self.p["card"], fg=self.p["text_muted"],
                font=("Segoe UI", 8), justify="left", wraplength=360,
            ).pack(anchor="w", pady=(4, 6))

        link_btn = tk.Button(
            inner, text="🔗 Get your free key", command=lambda: webbrowser.open(url),
            font=("Segoe UI", 8, "bold"), bg=self.p["accent"], fg="white",
            activebackground=self.p["accent_dark"], activeforeground="white",
            relief="flat", bd=0, cursor="hand2", padx=8, pady=4,
        )
        link_btn.pack(anchor="w", pady=(6, 6))

        if steps:
            steps_frame = tk.Frame(inner, bg=self.p["card"])
            steps_frame.pack(anchor="w", fill="x", pady=(0, 6))
            for i, step in enumerate(steps, 1):
                tk.Label(
                    steps_frame, text=f"{i}.  {step}", bg=self.p["card"], fg=self.p["text_muted"],
                    font=("Segoe UI", 8), justify="left", wraplength=360, anchor="w",
                ).pack(anchor="w", pady=1)

        entry = ttk.Entry(inner, textvariable=var, width=48, justify="left", show="•")
        entry.pack(anchor="w", fill="x")
        _fix_entry_keyboard_shortcuts(entry)

        status = tk.Label(
            inner, text="", bg=self.p["card"], font=("Segoe UI", 8, "bold"),
            justify="left", wraplength=360,
        )
        status.pack(anchor="w", pady=(4, 0))
        return status

    def _do_validate_and_save(self):
        gemini_val = self.gemini_var.get().strip()
        groq_val = self.groq_var.get().strip()
        nvidia_val = self.nvidia_var.get().strip()

        if not gemini_val or not groq_val:
            self.info_label.config(text="⚠ Both Gemini and Groq keys are required to continue.")
            return

        self.info_label.config(text="")
        self.gemini_status.config(text="")
        self.groq_status.config(text="")
        self.nvidia_status.config(text="")
        self.save_btn.config(state="disabled", text="⏳ Checking your key(s)...")
        threading.Thread(
            target=self._validate_worker, args=(gemini_val, groq_val, nvidia_val), daemon=True
        ).start()

    def _validate_worker(self, gemini_val: str, groq_val: str, nvidia_val: str):
        gemini_ok, gemini_err = _test_gemini_key(gemini_val)
        groq_ok, groq_err = _test_groq_key(groq_val)
        nvidia_ok, nvidia_err = (True, "") if not nvidia_val else _test_nvidia_key(nvidia_val)
        self.win.after(
            0, self._on_validated,
            gemini_val, gemini_ok, gemini_err,
            groq_val, groq_ok, groq_err,
            nvidia_val, nvidia_ok, nvidia_err,
        )

    def _on_validated(
        self, gemini_val, gemini_ok, gemini_err, groq_val, groq_ok, groq_err,
        nvidia_val, nvidia_ok, nvidia_err,
    ):
        self.save_btn.config(state="normal", text="💾  Save and Continue")

        self.gemini_status.config(
            text="✓ Valid" if gemini_ok else f"✗ Invalid: {gemini_err}",
            foreground=self.p["success"] if gemini_ok else self.p["danger"],
        )
        self.groq_status.config(
            text="✓ Valid" if groq_ok else f"✗ Invalid: {groq_err}",
            foreground=self.p["success"] if groq_ok else self.p["danger"],
        )
        if nvidia_val:
            self.nvidia_status.config(
                text="✓ Valid" if nvidia_ok else f"✗ Invalid: {nvidia_err}",
                foreground=self.p["success"] if nvidia_ok else self.p["danger"],
            )

        if not gemini_ok or not groq_ok:
            self.info_label.config(text="⚠ Fix the invalid key(s) above and try again - both are required.")
            return

        final_nvidia = nvidia_val if (nvidia_val and nvidia_ok) else ""

        try:
            _save_env(gemini_val, groq_val, final_nvidia)
        except Exception as e:
            self.info_label.config(text=f"⚠ Failed to save .env: {e}")
            return

        nvidia_rejected = bool(nvidia_val and not nvidia_ok)
        self.win.grab_release()
        self.win.destroy()
        if self.on_done:
            self.on_done(True, True, False, False, bool(final_nvidia), nvidia_rejected)

    def _on_cancel(self):
        # من غير Gemini وGroq، البرنامج مش هيقدر يعمل أي تفريغ/تلخيص أصلاً
        # - فبيتقفل بالكامل بدل ما يفتح بحالة ناقصة تلخبط المستخدم لاحقًا.
        self.win.destroy()
        self.root.destroy()


def show_dialog(root: tk.Tk, palette: dict, on_done=None) -> None:
    ensure_env_file_exists()
    _SetupDialog(root, palette, on_done)