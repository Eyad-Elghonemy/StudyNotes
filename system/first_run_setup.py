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
from pathlib import Path
from tkinter import messagebox, ttk

import process_lecture

PROJECT_DIR = Path(__file__).resolve().parent
ENV_PATH = PROJECT_DIR / ".env"
ENV_EXAMPLE_PATH = PROJECT_DIR / ".env.example"


def keys_configured() -> bool:
    """في مفتاح API واحد على الأقل متسجل فعليًا في الـ environment الحالية؟
    (بعد load_dotenv() اللي بتتنفذ أول ما البرنامج يفتح)."""
    return bool(os.environ.get("GEMINI_API_KEY", "").strip()) or bool(
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


def _save_env(gemini_key: str, groq_key: str) -> None:
    """يحدّث GEMINI_API_KEY/GROQ_API_KEY في .env من غير ما يلمس أي سطر
    تاني موجود فيه (زي STUDYNOTES_DIR)."""
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
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # نحدّث الـ environment الحالية وثوابت process_lecture مباشرة، لأنها
    # كانت اتقرت مرة واحدة بس وقت الـ import - من غير التحديث ده، البرنامج
    # هيفضل مش شايف المفاتيح الجديدة لحد ما يتعمله restart يدوي.
    os.environ["GEMINI_API_KEY"] = gemini_key
    os.environ["GROQ_API_KEY"] = groq_key
    process_lecture.GEMINI_API_KEY = gemini_key
    process_lecture.GROQ_API_KEY = groq_key


class _SetupDialog:
    def __init__(self, root: tk.Tk, palette: dict, on_done):
        self.root = root
        self.p = palette
        self.on_done = on_done

        self.win = tk.Toplevel(root)
        self.win.title("🔑 إعداد أول مرة")
        self.win.configure(bg=self.p["card"])
        self.win.resizable(False, False)
        self.win.transient(root)
        self.win.grab_set()
        self.win.protocol("WM_DELETE_WINDOW", self._on_cancel)

        pad = {"padx": 20, "pady": 6}

        ttk.Label(
            self.win,
            text="محتاج مفتاح API واحد على الأقل عشان يقدر يفرّغ ويلخّص المحاضرات.\n"
                 "تقدر تسيب أي مفتاح فاضي لو مش عندك، بس لازم واحد يتملى.",
            background=self.p["card"], foreground=self.p["text"],
            font=("Segoe UI", 10), justify="right", wraplength=420,
        ).pack(anchor="e", **pad)

        self.gemini_var = tk.StringVar()
        self.groq_var = tk.StringVar()
        self.gemini_status = self._field(
            "GEMINI_API_KEY", self.gemini_var,
            "احصل عليه من: https://ai.google.dev/",
        )
        self.groq_status = self._field(
            "GROQ_API_KEY", self.groq_var,
            "احصل عليه من: https://console.groq.com/",
        )

        self.info_label = ttk.Label(
            self.win, text="", background=self.p["card"], foreground=self.p["danger"],
            font=("Segoe UI", 9, "bold"), justify="right", wraplength=420,
        )
        self.info_label.pack(anchor="e", padx=20, pady=(4, 0))

        self.save_btn = tk.Button(
            self.win, text="💾 حفظ ومتابعة", command=self._do_validate_and_save,
            font=("Segoe UI", 10, "bold"), bg=self.p["accent"], fg="white",
            activebackground=self.p["accent_dark"], activeforeground="white",
            relief="flat", bd=0, cursor="hand2", padx=14, pady=8,
        )
        self.save_btn.pack(pady=16)

        self.win.update_idletasks()
        x = root.winfo_x() + (root.winfo_width() - self.win.winfo_width()) // 2
        y = root.winfo_y() + (root.winfo_height() - self.win.winfo_height()) // 2
        self.win.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _field(self, label_text, var, hint_text):
        box = ttk.Frame(self.win, style="Card.TFrame")
        box.pack(fill="x", padx=20, pady=4)
        ttk.Label(
            box, text=label_text, background=self.p["card"], foreground=self.p["text"],
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="e")
        entry = ttk.Entry(box, textvariable=var, width=46, justify="left", show="•")
        entry.pack(anchor="e", pady=(2, 0))
        ttk.Label(
            box, text=hint_text, background=self.p["card"], foreground=self.p["text_muted"],
            font=("Segoe UI", 8),
        ).pack(anchor="e")
        status = ttk.Label(
            box, text="", background=self.p["card"], font=("Segoe UI", 8, "bold"),
            justify="right", wraplength=420,
        )
        status.pack(anchor="e")
        return status

    def _do_validate_and_save(self):
        gemini_val = self.gemini_var.get().strip()
        groq_val = self.groq_var.get().strip()

        if not gemini_val and not groq_val:
            self.info_label.config(text="⚠ لازم تدخل مفتاح واحد على الأقل.")
            return

        self.info_label.config(text="")
        self.gemini_status.config(text="")
        self.groq_status.config(text="")
        self.save_btn.config(state="disabled", text="⏳ بيتأكد من صحة المفتاح/المفاتيح...")
        threading.Thread(
            target=self._validate_worker, args=(gemini_val, groq_val), daemon=True
        ).start()

    def _validate_worker(self, gemini_val: str, groq_val: str):
        gemini_ok, gemini_err = (True, "") if not gemini_val else _test_gemini_key(gemini_val)
        groq_ok, groq_err = (True, "") if not groq_val else _test_groq_key(groq_val)
        self.win.after(
            0, self._on_validated, gemini_val, gemini_ok, gemini_err, groq_val, groq_ok, groq_err
        )

    def _on_validated(self, gemini_val, gemini_ok, gemini_err, groq_val, groq_ok, groq_err):
        self.save_btn.config(state="normal", text="💾 حفظ ومتابعة")

        if gemini_val:
            self.gemini_status.config(
                text="✓ صالح" if gemini_ok else f"✗ غير صالح: {gemini_err}",
                foreground=self.p["success"] if gemini_ok else self.p["danger"],
            )
        if groq_val:
            self.groq_status.config(
                text="✓ صالح" if groq_ok else f"✗ غير صالح: {groq_err}",
                foreground=self.p["success"] if groq_ok else self.p["danger"],
            )

        final_gemini = gemini_val if (gemini_val and gemini_ok) else ""
        final_groq = groq_val if (groq_val and groq_ok) else ""

        if not final_gemini and not final_groq:
            self.info_label.config(text="⚠ مفيش أي مفتاح صالح - صلّح البيانات وحاول تاني.")
            return

        try:
            _save_env(final_gemini, final_groq)
        except Exception as e:
            self.info_label.config(text=f"⚠ فشل حفظ ملف .env: {e}")
            return

        gemini_rejected = bool(gemini_val and not gemini_ok)
        groq_rejected = bool(groq_val and not groq_ok)
        self.win.grab_release()
        self.win.destroy()
        if self.on_done:
            self.on_done(bool(final_gemini), bool(final_groq), gemini_rejected, groq_rejected)

    def _on_cancel(self):
        # حسب المتطلب: من غير مفتاح API واحد على الأقل، البرنامج مش هيقدر
        # يعمل أي تفريغ/تلخيص أصلاً - فبيتقفل بالكامل بدل ما يفتح بحالة
        # ناقصة تلخبط المستخدم لاحقًا.
        self.win.destroy()
        self.root.destroy()


def show_dialog(root: tk.Tk, palette: dict, on_done=None) -> None:
    ensure_env_file_exists()
    _SetupDialog(root, palette, on_done)