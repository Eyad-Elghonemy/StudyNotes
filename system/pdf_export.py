"""
تحويل HTML (النوتس بعد ما اتحوّلت من Markdown) لملف PDF.

الفكرة: بدل ما نزوّد مكتبة PDF تقيلة (وبتبوّظ العربي/الـ RTL وبتكبّر الـ exe)،
بنستخدم المتصفح اللي موجود أصلاً على جهاز اليوزر - Microsoft Edge جاي مع
ويندوز 10/11 نفسه، أو Chrome لو متثبت - في وضع "headless" (من غير ما يفتح
نافذة) وبنقوله يطبع الصفحة PDF. ده بيدّي نفس شكل النوتس بالظبط (عربي RTL
صح، تلوين الكود، صناديق التمييز، المعادلات) من غير أي اعتماديات جديدة.

بتستخدم مكتبات بايثون القياسية بس (subprocess / tempfile / pathlib).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class PdfExportError(Exception):
    """فشل التحويل (رسالة الخطأ مكتوبة بشكل مفهوم لليوزر)."""


class BrowserNotFound(PdfExportError):
    """مفيش Edge ولا Chrome على الجهاز."""


def find_browser() -> Path | None:
    """يدوّر على Edge الأول (موجود دايماً على ويندوز 10/11) وبعدين Chrome."""
    candidates: list[Path] = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData"):
        base = os.environ.get(env)
        if not base:
            continue
        candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        base = os.environ.get(env)
        if not base:
            continue
        candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")

    for c in candidates:
        if c.is_file():
            return c

    # لو متثبتين في مكان غير عادي (أو على نظام غير ويندوز وقت التطوير)
    for name in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def html_to_pdf(html: str, pdf_path: Path, timeout: int = 120, browser: Path | None = None) -> None:
    """
    يكتب الـ html في ملف مؤقت، ويشغّل المتصفح headless عشان يطبعه PDF في
    pdf_path. بيرمي BrowserNotFound أو PdfExportError لو فشل.
    """
    browser = browser or find_browser()
    if browser is None:
        raise BrowserNotFound("مفيش Microsoft Edge ولا Google Chrome على الجهاز.")

    pdf_path = Path(pdf_path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    # بنعمل الفولدر المؤقت يدوي (مش عن طريق "with TemporaryDirectory")
    # عمدًا: مسح الفولدر ده بعد ما Edge/Chrome يتقفل ممكن يفشل مؤقتًا لو
    # الـ processes الفرعية بتاعته (GPU process, crashpad handler...) لسه
    # ماسكة ملفات جواه لحظات بعد القفل - ده مش معناه إن التحويل فشل (الـ
    # PDF بيكون اتحفظ في مكانه النهائي فعلاً قبل المحاولة دي)، فمينفعش
    # نسيب فشل التنظيف ده يبوّظ نتيجة العملية اللي نجحت أصلاً.
    tmp = tempfile.mkdtemp(prefix="studynotes_pdf_")
    try:
        tmp_dir = Path(tmp)
        html_file = tmp_dir / "notes.html"
        html_file.write_text(html, encoding="utf-8")
        tmp_pdf = tmp_dir / "notes.pdf"

        cmd = [
            str(browser),
            "--headless=new",
            "--disable-gpu",
            # كروميوم افتراضيًا مابيكتبش أي حاجة على stderr/stdout خالص حتى
            # لو حصل خطأ داخلي - من غير السطرين دول، ملف اللوج هيفضل فاضي
            # دايمًا (زي ما حصل) بغض النظر عن السبب الحقيقي للمشكلة.
            "--enable-logging=stderr",
            "--v=1",
            # profile مؤقت منفصل: لو Edge/Chrome شغال عند اليوزر بالفعل، من غيره
            # الأمر بيتحوّل للنافذة المفتوحة وبيرجع فورًا من غير ما يطبع أي PDF.
            f"--user-data-dir={tmp_dir / 'profile'}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
            # بدون الهيدر/الفوتر الافتراضي (التاريخ والـ URL فوق وتحت الصفحة)؛
            # الاسمين لأن الفلاج اتغيّر اسمه بين إصدارات كروميوم - اللي مش
            # معروف بيتتجاهل من غير مشاكل.
            "--no-pdf-header-footer",
            "--print-to-pdf-no-header",
            f"--print-to-pdf={tmp_pdf}",
            html_file.as_uri(),
        ]
        if sys.platform.startswith("linux"):
            cmd.insert(1, "--no-sandbox")  # لبيئات التطوير/الاختبار بس (root)

        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # من غير كونسول بيومض

        # بنسجّل مخرجات المتصفح (stderr) في ملف بدل ما نكتمها خالص - لو
        # فشل التحويل، بنقدر نعرض آخر أسطر منه في رسالة الخطأ عشان نعرف
        # السبب الحقيقي بدل رسالة عامة "خلص من غير PDF".
        log_file = tmp_dir / "browser_log.txt"
        with open(log_file, "wb") as log_f:
            proc = subprocess.Popen(
                cmd, stdout=log_f, stderr=subprocess.STDOUT, **kwargs
            )
        try:
            deadline = time.monotonic() + timeout
            last_size = -1
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                # بعض الإصدارات بتكتب الملف وتفضل شغالة لحظات - لو الملف ظهر
                # وحجمه ثبت، مفيش داعي نستنى العملية تخلص بنفسها.
                if tmp_pdf.exists():
                    size = tmp_pdf.stat().st_size
                    if size > 0 and size == last_size:
                        break
                    last_size = size
                time.sleep(0.4)
            else:
                raise PdfExportError("التحويل خد وقت أطول من اللازم وتم إيقافه.")
        finally:
            if proc.poll() is None:
                _kill_process_tree(proc)

        # مستنى شوية إضافية بعد ما العملية "خلصت" ظاهريًا: على ويندوز،
        # Edge/Chrome بيسيبوا وراهم processes فرعية (GPU process, crashpad
        # handler...) بتاخد لحظات تانية عشان تقفل فعليًا وتسيب قفلها على
        # ملفات فولدر الـ profile - لو حاولنا نمسح الفولدر فورًا ممكن نلاقي
        # "WinError 32" حتى لو الـ PDF نفسه اتكتب صح خلاص.
        time.sleep(0.5)

        if not tmp_pdf.exists() or tmp_pdf.stat().st_size == 0:
            detail = ""
            try:
                log_text = log_file.read_text(encoding="utf-8", errors="ignore").strip()
                if log_text:
                    detail = "\n\nتفاصيل من المتصفح:\n" + log_text[-800:]
            except OSError:
                pass
            code_info = f"\n\nكود خروج المتصفح: {proc.returncode}"
            raise PdfExportError("المتصفح خلص من غير ما يطلّع ملف PDF." + code_info + detail)

        try:
            # move بدل rename: ممكن الوجهة على درايف تاني غير الـ temp
            shutil.move(str(tmp_pdf), str(pdf_path))
        except PermissionError:
            raise PdfExportError(
                "مقدرش أكتب الملف - غالبًا الـ PDF القديم مفتوح في برنامج تاني. اقفله وجرب تاني."
            )
        except OSError as e:
            raise PdfExportError(f"مقدرش أحفظ ملف الـ PDF: {e}")
    finally:
        # تنظيف الفولدر المؤقت "بأفضل مجهود" - لو فشل (ملف لسه مقفول من
        # process فرعية لسه بتقفل)، بنحاول كذا مرة بفاصل بسيط، وفي الآخر
        # لو فضل يفشل بنسيبه (الفولدر جوه Temp أصلاً وهيتنضف بمرور الوقت)
        # من غير ما نرمي استثناء يخلي العملية كلها تبان فاشلة وهي نجحت.
        for attempt in range(4):
            try:
                shutil.rmtree(tmp, ignore_errors=False)
                break
            except OSError:
                if attempt == 3:
                    break
                time.sleep(0.5)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """بتقفل الـ process الأساسي وكل الـ processes الفرعية بتاعته (GPU
    process, crashpad handler, renderer...) اللي Edge/Chrome بيشغلها.
    proc.kill() لوحدها بتقفل الـ process الأساسي بس - الفرعيين بيفضلوا
    شغالين شوية على ويندوز، وده اللي بيسبب قفل ملفات فولدر الـ profile
    المؤقت بعد كده."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass
