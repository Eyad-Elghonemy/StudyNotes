"""
إدارة الحالة المشتركة بين كل السكريبتات: أسماء المحاضرات، حالة كل جزء
صوتي (متسجل/متفرغ/متشرّح)، ولحد فين وصل التلخيص. الحالة متخزنة في ملفات
JSON صغيرة جوه فولدر ".state" جنب المشروع عشان نقدر نكمل من حيث وقفنا
في أي وقت.

ملحوظة: كل الفولدرات (Sound_Recorded, Transcript, Markdown, .state) بيتم
إنشاؤها أوتوماتيك أول ما أي سكريبت يتشغل - مفيش داعي تعملهم يدوي.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# ============================================================================
# حماية قسرية ضد مشكلة tornado/tenacity المعروفة:
# tenacity (تبعية فرعية لـ google-genai) بتحاول تستورد tornado (مكتبة
# اختيارية إحنا مش مستخدمينها خالص) جوه try/except ImportError - عادي
# ومضمون وقت التطوير. لكن جوه الـ exe المجمّع بـ PyInstaller، ظهرت حالات
# إن tornado بتتبني كـ "حزمة فاضية/ناقصة" (مش غائبة تمامًا) رغم استبعادها
# من الـ spec، وده بيخلّي الاستيراد الداخلي بتاعها يفشل بشكل تاني (مش
# ModuleNotFoundError العادي اللي tenacity بتتوقعه وتتعامل معاه)، فالخطأ
# بيطلع لليوزر بدل ما يتلقط بهدوء.
#
# الحل: نحط None في sys.modules['tornado'] يدوي *قبل* أي حد يحاول
# يستوردها - بايثون وقتها بيرمي ImportError نضيف فورًا لأي حد يعمل
# "import tornado"، وده بالظبط اللي tenacity متوقعة وبتتعامل معاه صح.
# إحنا مش بنستخدم tornado في أي حتة من الكود، فمفيش أي خطورة من المنع ده.
sys.modules.setdefault("tornado", None)
# ============================================================================

# مكان الكود ومكان البيانات (اتنين منفصلين):
#
# - BASE_DIR: مكان ملف .env (مفاتيح الـ API). وقت التطوير = فولدر الكود نفسه.
# - DATA_DIR: مكان فولدرات البيانات (Sound_Recorded / Transcript / Markdown /
#   .state). بيتحدد بالترتيب ده:
#     1) متغيّر بيئة STUDYNOTES_DIR على مستوى ويندوز (لو متظبط).
#     2) لو شغال كـ .exe مجمّع: AppData\Local\StudyNotes (بعيد عن Program Files
#        عشان الكتابة تشتغل من غير صلاحيات Admin).
#     3) سطر STUDYNOTES_DIR=... جوه ملف .env جنب الكود (وقت التطوير) - ده اللي
#        بيخلّي البيانات تتحط في فولدر برّه فولدر الكود، وتتعمل تلقائي لو مش موجودة.
#     4) لو مفيش أي حاجة من دول: فولدر الكود نفسه (زي الأول).
def _studynotes_dir_from_dotenv(env_file: Path) -> Path | None:
    """يقرأ سطر STUDYNOTES_DIR من ملف .env بشكل يدوي بسيط (من غير أي مكتبة)،
    لأن .env نفسه لسه متحمّلش وقت حساب المسارات هنا."""
    try:
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "STUDYNOTES_DIR":
                value = value.strip().strip('"').strip("'").strip()
                return Path(os.path.expandvars(value)).expanduser() if value else None
    except OSError:
        pass
    return None


_CODE_DIR = Path(__file__).resolve().parent

if os.environ.get("STUDYNOTES_DIR"):
    BASE_DIR = Path(os.environ["STUDYNOTES_DIR"])
    DATA_DIR = BASE_DIR
elif getattr(sys, "frozen", False):
    BASE_DIR = Path(os.environ["LOCALAPPDATA"]) / "StudyNotes"
    DATA_DIR = BASE_DIR
else:
    BASE_DIR = _CODE_DIR
    DATA_DIR = _studynotes_dir_from_dotenv(_CODE_DIR / ".env") or BASE_DIR

RECORD_FOLDER = DATA_DIR / "Sound_Recorded"
TRANSCRIPT_FOLDER = DATA_DIR / "Transcript"
MARKDOWN_FOLDER = DATA_DIR / "Markdown"
STATE_FOLDER = DATA_DIR / ".state"

try:
    for folder in (RECORD_FOLDER, TRANSCRIPT_FOLDER, MARKDOWN_FOLDER, STATE_FOLDER):
        folder.mkdir(parents=True, exist_ok=True)
except OSError as _e:
    # مثلاً الدرايف المحدد (D:) مش موجود دلوقتي - نرجع لفولدر الكود بدل ما
    # البرنامج يقع خالص، ونطبع تحذير واضح.
    print(f"[!] مقدرتش أستخدم فولدر البيانات {DATA_DIR} ({_e}). هستخدم فولدر الكود بدله.")
    DATA_DIR = BASE_DIR = _CODE_DIR
    RECORD_FOLDER = DATA_DIR / "Sound_Recorded"
    TRANSCRIPT_FOLDER = DATA_DIR / "Transcript"
    MARKDOWN_FOLDER = DATA_DIR / "Markdown"
    STATE_FOLDER = DATA_DIR / ".state"
    for folder in (RECORD_FOLDER, TRANSCRIPT_FOLDER, MARKDOWN_FOLDER, STATE_FOLDER):
        folder.mkdir(parents=True, exist_ok=True)


def safe_name(name: str) -> str:
    """تنضيف اسم المحاضرة من رموز ممنوعة في أسماء الملفات على ويندوز."""
    cleaned = "".join(c for c in name if c not in r'\/:*?"<>|').strip()
    return cleaned or "untitled_lecture"


def _glob_lecture_audio(lecture: str, ext: str) -> list[Path]:
    """بديل آمن لـ RECORD_FOLDER.glob(f"{lecture}__*.{ext}") - بيقارن اسم
    الملف حرفيًا (startswith + endswith) بدل ما يمرّر اسم المحاضرة كـ
    glob pattern. safe_name() بتسمح بالقوسين [ ] في اسم المحاضرة (مش
    رموز ممنوعة في أسماء ملفات ويندوز)، لكن pathlib.Path.glob() بتفسّر
    [...] كـ character-class matching (زي RegEx) مش كنص حرفي - فمحاضرة
    اسمها فيه قوس كانت ملفاتها "تختفي" من كل القوايم (مدة 0، مفيش أجزاء
    ظاهرة) رغم إنها موجودة فعليًا على القرص (BUG-04 في التقرير الجنائي).
    """
    prefix = f"{lecture}__"
    suffix = f".{ext}"
    if not RECORD_FOLDER.exists():
        return []
    return [
        p for p in RECORD_FOLDER.iterdir()
        if p.is_file() and p.name.startswith(prefix) and p.name.endswith(suffix)
    ]


# أسماء ملفات .json موجودة جوه STATE_FOLDER لكنها مش محاضرات - بتخص
# وحدات تانية (evaluation.py, remote_config.py) بتشارك نفس الفولدر لغرض
# تاني خالص. من غير الاستبعاد ده، list_existing_lectures() كانت بتعامل
# "pending_feedback" كأنه اسم محاضرة، و load_state("pending_feedback")
# كانت بتفشل أو بترجع بيانات مالهاش معنى (محتواها list أصلًا بالتصميم -
# طابور تقييمات لسه ما اتبعتتش - مش حالة محاضرة خالص).
_RESERVED_STATE_FILENAMES = {"pending_feedback", "remote_config_cache"}


def list_existing_lectures() -> list[str]:
    """
    يرجع أسماء كل المحاضرات المعروفة، سواء كان ليها ملف حالة (بعد أول
    تفريغ ناجح) أو لسه بس ملفات صوت مسجلة.
    """
    from_state = {p.stem for p in STATE_FOLDER.glob("*.json")} - _RESERVED_STATE_FILENAMES
    from_audio = set()
    for ext in ("opus", "flac", "wav"):
        for p in RECORD_FOLDER.glob(f"*__*.{ext}"):
            name = p.stem.rsplit("__", 1)[0]
            from_audio.add(name)
    return sorted(from_state | from_audio)


def load_state(lecture: str) -> dict:
    path = STATE_FOLDER / f"{lecture}.json"
    data = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                # الملف اتقرا كـ JSON صحيح من غير أي استثناء، لكن القيمة
                # اللي جواه مش dict (مثلاً list) - شكل غلط تمامًا عن
                # المتوقع، مش "ملف تالف" بالمعنى اللي except تحت بتمسكه.
                # من غير الفحص ده، data.setdefault() تحت كانت بترمي
                # AttributeError وتطيّح البرنامج (كراش حقيقي حصل في
                # البروداكشن) كل مرة اليوزر يفتح المحاضرة دي. بنعاملها
                # زي ملف تالف بالظبط: تحذير في اللوج ورجوع لقيم افتراضية.
                raise ValueError(f"القيمة الجذرية {type(data).__name__} مش dict")
        except (json.JSONDecodeError, OSError, ValueError) as e:
            # ملف الـ state اتلف (مثلاً الجهاز اتقفل فجأة وسط الكتابة
            # القديمة قبل إصلاح save_state تحت) - من غير الحماية دي، أي
            # محاولة فتح للمحاضرة دي كانت بتطيّح البرنامج بالكامل (crash)
            # من غير ما نقدر نفتحها تاني أبدًا. بدل كده: نحذّر في اللوج
            # ونرجّع للقيم الافتراضية (زي لو المحاضرة دي أول مرة) - أحسن
            # من كراش كامل، حتى لو معناها فقدان آخر تحديثات متسجلة.
            print(
                f"[!] تحذير: ملف حالة المحاضرة \"{lecture}\" تالف أو مش قابل "
                f"للقراءة ({e}) - هيتعامل معاها كمحاضرة جديدة. لو كانت فيها "
                "تقدّم سابق، ممكن يكون ضاع."
            )
            data = {}
    # قيم افتراضية لأي حقل جديد اتضاف بعدين، عشان الملفات القديمة تفضل شغالة
    data.setdefault("transcribed_files", [])  # اتفرغت (بس مش بالضرورة اتشرحت)
    data.setdefault("explained_files", [])    # اتفرغت واتشرحت كمان
    data.setdefault("summarized_chars", 0)    # لحد أي حرف في الترانسكريبت اتشرح
    # مكان كل ملف صوت جوه ملف الترانسكريبت التراكمي: {filename: [start, end]}
    # (بالحروف مش البايتات) - بيتسجل وقت التفريغ في process_lecture.py،
    # ومستخدم عشان نقدر نمسح تفريغ ملف واحد بعينه من غير ما نبوظ الباقي.
    data.setdefault("transcript_ranges", {})
    # مجال/مادة المحاضرة (طب، قانون، برمجة...إلخ) - بيتحدد وقت إنشاء
    # المحاضرة من الواجهة، وبيتستخدم لتخصيص برومبت التلخيص. فاضي = غير
    # محدد، وبيترجم لـ"برمجة/علوم حاسب" (السلوك الافتراضي الأصلي، من غير
    # أي تغيير) - عشان المحاضرات القديمة تفضل شغالة بالظبط زي ما هي.
    data.setdefault("subject", "")
    # صناديق 🔧 تصحيح و💬 إضافة الاختياريين - إعداد خاص بكل محاضرة لوحدها
    # (مش عام لكل المحاضرات)، بيتحدد وقت إنشاء المحاضرة. enable_corrections
    # افتراضيًا True (Bug #02 في تقرير المراجعة: اتوعدنا في الـ changelog
    # إنها بقت افتراضية، بس الـ checkbox القديم اتشال من الواجهة من غير ما
    # حد يغيّر القيمة الافتراضية هنا - فضلت الميزة معطّلة بالكامل رغم
    # الوعد). enable_additions لسه افتراضيًا False (مفيش وعد مشابه بيها).
    data.setdefault("enable_corrections", True)
    data.setdefault("enable_additions", False)
    return data


def save_state(lecture: str, state: dict) -> None:
    """بيكتب ملف الـ state بشكل atomic: بيكتب على ملف مؤقت الأول، وبعد
    ما الكتابة تخلص بنجاح بالكامل، بيستبدل الملف الأصلي بيه دفعة واحدة
    (os.replace). العملية دي ذرّية على مستوى نظام التشغيل - يعني لو
    البرنامج اتقفل فجأة (قفل جهاز، انقطاع كهرباء) في أي لحظة أثناء
    الكتابة، إما الملف القديم يفضل زي ما هو بالكامل، أو الجديد يحل
    محله بالكامل - مفيش احتمال نتيجة "نص مكتوبة" تطيّح البرنامج بعد كده
    (Bug #03 في تقرير المراجعة)."""
    path = STATE_FOLDER / f"{lecture}.json"
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())  # نتأكد المحتوى فعليًا وصل للقرص قبل الـ rename
    os.replace(tmp_path, path)


# =========================================================================
# قفل لكل محاضرة: بيمنع تعارض لو حصلت أكتر من عملية تفريغ/تلخيص في نفس
# الوقت لنفس المحاضرة (مثلاً دبل كليك على زرار المعالجة)، عشان يتجنب
# فقدان تحديثات في ملف الـ state أو تداخل الكتابة في ملف الترانسكريبت.
# =========================================================================
_state_locks: dict[str, threading.Lock] = {}
_state_locks_guard = threading.Lock()


def get_lecture_lock(lecture: str) -> threading.Lock:
    with _state_locks_guard:
        if lecture not in _state_locks:
            _state_locks[lecture] = threading.Lock()
        return _state_locks[lecture]


def pick_lecture_name() -> str:
    """CLI فقط: يعرض المحاضرات الموجودة ويسيب المستخدم يختار أو يكتب واحدة جديدة."""
    existing = list_existing_lectures()
    if existing:
        print("\nالمحاضرات الموجودة بالفعل:")
        for i, name in enumerate(existing, 1):
            print(f"  {i}) {name}")
        print("  0) محاضرة جديدة (اكتب اسم جديد)")

        choice = input("اختر رقم المحاضرة، أو 0 لمحاضرة جديدة: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(existing):
            return existing[int(choice) - 1]

    new_name = input("اسم المحاضرة/الكورس الجديدة: ").strip()
    return safe_name(new_name)


# مسار ffmpeg المكتشف فعليًا (سواء المضموم جوه البرنامج أو من PATH العام)،
# بيتحسب مرة واحدة بس وبيتخزّن هنا عشان مانكررش البحث كل مرة.
_FFMPEG_CMD: str | None = None


def _bundled_ffmpeg_path() -> Path | None:
    """لو البرنامج شغال كـ .exe مجمّع، PyInstaller بيحط أي binaries
    (زي ffmpeg.exe) جوه فولدر _internal جنب الـ exe نفسه - هنا بندوّر
    عليه هناك الأول قبل PATH العام."""
    if not getattr(sys, "frozen", False):
        return None
    candidate = Path(sys.executable).resolve().parent / "_internal" / "ffmpeg.exe"
    return candidate if candidate.exists() else None


def ffmpeg_available() -> bool:
    global _FFMPEG_CMD
    if _FFMPEG_CMD:
        return True
    bundled = _bundled_ffmpeg_path()
    if bundled:
        _FFMPEG_CMD = str(bundled)
        return True
    if shutil.which("ffmpeg") is not None:
        _FFMPEG_CMD = "ffmpeg"
        return True
    return False


# الحد الأدنى للمساحة الفاضية قبل ما نحذر المستخدم قبل بدء التسجيل (بالميجا).
# FLAC خام بمعدل 16kHz/mono بياخد تقريباً ~1.8 ميجا/دقيقة، يعني 500 ميجا
# بتغطي كذا ساعة تسجيل قبل أول ضغط لـ Opus - رقم متحفظ مش دقيق 100%.
LOW_DISK_WARNING_MB = 500


def check_disk_space_mb() -> float:
    """بيرجع المساحة الفاضية بالميجابايت على نفس الـ drive بتاع RECORD_FOLDER.
    بيرجع -1 لو فشل الفحص (بدل ما يوقف التسجيل بسبب فحص فشل)."""
    try:
        usage = shutil.disk_usage(RECORD_FOLDER)
        return usage.free / (1024 * 1024)
    except Exception:
        return -1


def compress_to_opus(src_path: Path, bitrate: str = "24k") -> Path:
    """
    يضغط ملف صوتي (FLAC/WAV) لصيغة Opus. بيرجع مسار الملف الجديد.
    لو ffmpeg مش موجود، أو الضغط فشل، أو الناتج ناقص عن الأصل، بيرجع
    نفس المسار الأصلي (الـ FLAC) - عشان نضمن مفيش صوت بيضيع بصمت أبدًا.

    بيقارن مدة الملف الناتج بالأصلي قبل ما يمسح الأصلي؛ لو الناتج أقصر
    بشكل ملحوظ، بيسيب الملفين الاتنين ويرجّع الأصلي بدل ما يمسحه.

    بيتأكد فعلياً إن الملف الأصلي اتمسح بنجاح (مع إعادة محاولة قصيرة لو
    كان لسه مقفول من عملية تانية)، عشان مايفضلش نسختين من نفس الجزء
    (FLAC + Opus) بيتفرغوا مرتين بالغلط.
    """
    if not ffmpeg_available():
        return src_path

    out_path = src_path.with_suffix(".opus")
    # ffmpeg بيكتب على اسم مؤقت (.opus.tmp) مش على الاسم النهائي مباشرة؛
    # الاسم النهائي (out_path) مابيظهرش على القرص إلا بعد ما ننجح نتأكد
    # من اكتمال وصحة الناتج بالكامل (os.replace لحظي واحد تحت). من غير
    # كده كانت فيه نافذة زمنية حقيقية (كذا ثانية، طول مدة التحويل) فيها
    # ملف .opus ناقص موجود على الاسم النهائي نفسه اللي أي كود تاني ممكن
    # يحاول يقراه وقتها (BUG-05 في التقرير الجنائي - ده السبب الجذري
    # اللي Bug #02 عالج عرضه بس، مش جذره، بإضافة fallback للـ FLAC
    # الأصلي وقت القراءة).
    tmp_out_path = out_path.with_suffix(out_path.suffix + ".tmp")
    try:
        # creationflags=CREATE_NO_WINDOW: من غير ده، كل استدعاء لـ ffmpeg
        # وقت ما البرنامج شغال كـ exe (windowed, من غير console) كان بيفتح
        # نافذة Console سودا تومض لثانية وتختفي.
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.run(
            [
                _FFMPEG_CMD or "ffmpeg",
                # بنجبر ffmpeg يحلل الملف كامل فعليًا (مش يثق في "الطول"
                # المكتوب في الـ Header بس) قبل ما يبدأ التحويل - ده بيحمي
                # من حالات نادرة فيها الـ Header بيقول "طول غير معروف" أو
                # غلط رغم إن الصوت الفعلي كله موجود وكامل جوه الملف؛ من
                # غيره بعض نسخ ffmpeg بتوقف عند الطول المكتوب غلط بدل آخر
                # الملف الفعلي. القيم دي (بالبايت/ميكروثانية) كبيرة كفاية
                # لأي ملف تسجيل محاضرة واقعي (لحد كذا ساعة).
                "-analyzeduration", "100M", "-probesize", "100M",
                "-y", "-i", str(src_path),
                "-ar", "16000", "-ac", "1",
                "-c:a", "libopus", "-b:a", bitrate,
                # -f opus لازمة هنا بالتحديد: ffmpeg بيحدد نوع الـ
                # container افتراضيًا من امتداد الملف، والاسم المؤقت
                # بينتهي بـ ".tmp" (مش ".opus")، فمن غير فرض النوع صراحة
                # ffmpeg بيرفض يشتغل أصلًا ("Unable to choose an output
                # format"). من غير التعديل ده، إصلاح BUG-05 نفسه كان
                # هيبوّظ الضغط بالكامل.
                "-f", "opus",
                str(tmp_out_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
        if not tmp_out_path.exists() or tmp_out_path.stat().st_size == 0:
            tmp_out_path.unlink(missing_ok=True)
            return src_path  # الضغط فشل فعلياً رغم مفيش exception

        # تحقّق إضافي مهم: الملف "موجود ومش فاضي" مش كفاية لوحدها - ffmpeg
        # ممكن (نادراً، بسبب نسخة/بناء معين منه) يطلّع ملف Opus صحيح لكن
        # بمدة أقصر من الأصل (بيانات صوت ناقصة فعليًا). لو مسحنا الـ FLAC
        # الأصلي في الحالة دي، البيانات الضايعة مش هترجع تاني - فبنقارن
        # المدتين الأول، ومش بنمسح الأصل غير لو فعلاً متطابقين (بهامش
        # بسيط للفروق الطبيعية جدًا في الترميز).
        src_duration = _audio_duration_seconds(src_path)
        out_duration = _audio_duration_seconds(tmp_out_path)
        if (
            src_duration is not None and out_duration is not None
            and out_duration < src_duration - 2  # هامش ثانيتين لفروق الترميز الطبيعية
        ):
            # الضغط "نجح" تقنيًا بس الناتج ناقص عن الأصل - نمسح المؤقت
            # ونسيب الـ FLAC الأصلي زي ما هو، ونرجّعه عشان الاستخدام
            # الفعلي (تفريغ/تلخيص) يكمل بالملف الكامل، ومفيش بيانات
            # بتتفقد بصمت.
            tmp_out_path.unlink(missing_ok=True)
            return src_path

        # التبديل اللحظي (atomic): من هنا بس الاسم النهائي (out_path)
        # بيظهر على القرص، وبمحتوى كامل ومتحقق منه بالفعل - مفيش أي
        # لحظة وسطى فيها ملف ناقص على out_path.
        os.replace(tmp_out_path, out_path)

        # نحاول نمسح الأصلي كذا مرة لو كان لسه مقفول من ثريد تاني
        for attempt in range(5):
            try:
                src_path.unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.5)
        return out_path
    except Exception:
        tmp_out_path.unlink(missing_ok=True)
        return src_path


def repair_corrupted_audio(src_path: Path) -> Path | None:
    """
    يحاول إصلاح ملف صوتي تالف (خاصة ملفات FLAC الناتجة عن إغلاق مفاجئ للبرنامج
    حيث يحتوي الملف على بيانات الصوت كاملة وسليمة لكن الـ FLAC header غير مكتمل و duration N/A).

    خطوات الإصلاح:
    1. تحويل الملف مباشرة إلى صيغة .opus القياسية للمشروع مع -analyzeduration 100M -probesize 100M
       وإعادة كتابة الـ container كـ Ogg Opus سليم مع حساب دقيق للمدة الزمنية.
    2. فحص مدة ملف الـ Opus الناتج للتأكد بنسبة 100% أنه صالح وسليم ورقم حقيقي.
    3. إذا نجح، يتم نقل الملف التالف الأصلي لمجلد احتياطي منفصل _corrupted_backup/
       لحمايته من الضياع، مع التبديل الذري atomic rename للـ Opus النهائي.
    4. يرجع المسار الجديد الصالح (.opus)، أو None إذا فشل الإصلاح.
    """
    if not src_path.exists() or src_path.stat().st_size == 0:
        return None
    if not ffmpeg_available():
        return None

    # نحدد مجلد النسخ الاحتياطي للملفات التالفة لحمايتها من الضياع
    backup_dir = RECORD_FOLDER / "_corrupted_backup"
    backup_dir.mkdir(parents=True, exist_ok=True)

    out_opus = src_path.with_suffix(".opus")
    tmp_out = out_opus.with_suffix(".opus.repair.tmp")
    try:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        # تحويل مباشر لـ Opus مع تحليل كامل للملف (حتى لو الـ header الأصلي ناقص)
        subprocess.run(
            [
                _FFMPEG_CMD or "ffmpeg",
                "-analyzeduration", "100M", "-probesize", "100M",
                "-y", "-i", str(src_path),
                "-ar", "16000", "-ac", "1",
                "-c:a", "libopus", "-b:a", "24k",
                "-f", "opus",
                str(tmp_out),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )

        if not tmp_out.exists() or tmp_out.stat().st_size == 0:
            tmp_out.unlink(missing_ok=True)
            return None

        # فحص هل المدة الناتجة صالحة وموجبة؟
        dur = _audio_duration_seconds(tmp_out)
        if dur is None or dur <= 0:
            tmp_out.unlink(missing_ok=True)
            return None

        # نقل الملف التالف الأصلي للأرشيف الاحتياطي بأمان
        archived_target = backup_dir / src_path.name
        if archived_target.exists():
            archived_target = backup_dir / f"{src_path.stem}_{int(time.time())}{src_path.suffix}"
        try:
            shutil.move(str(src_path), str(archived_target))
        except Exception:
            pass

        # استبدال ذري بالملف النهائي
        os.replace(tmp_out, out_opus)
        return out_opus

    except Exception:
        tmp_out.unlink(missing_ok=True)
        return None


# =========================================================================
# تتبع حالة الأجزاء الصوتية (recorded / transcribed / explained)
# =========================================================================

# حد أقصى منطقي لمدة أي جزء صوتي واحد بالدقايق. أي جزء أطول من كده فعليًا
# مستحيل (الأجزاء بتتقفل تلقائي كل 30 دقيقة أثناء التسجيل - راجع
# CHUNK_MINUTES في gui_app.py/record_session.py)، فأي رقم أكبر بكتير من
# الحد ده معناه إن الملف تالف (header فيه قيمة garbage غالبًا بسبب قفل
# غير سليم للملف - مثلاً البرنامج اتقفل فجأة أو اتقطع التيار وسط الكتابة)
# - مش رقم حقيقي محتاج نقصّه/نقرّبه، لازم نعامله كبيانات تالفة صراحة بدل
# ما نعرضه أو نحسبه في أي إجمالي. الحد هنا أكبر من الـ 30 دقيقة الفعلية
# بهامش أمان (لو القيمة اتغيرت لاحقًا)، مش نسخة مكررة من نفس الرقم.
MAX_SANE_CHUNK_MINUTES = 120


def _audio_duration_seconds(path: Path) -> float | None:
    """يرجع المدة بالثواني، أو None لو الملف تالف/المدة غير منطقية على
    الإطلاق (بدل ما نرجع رقم غلط زي ما هو أو صفر مضلل)."""
    try:
        import soundfile as sf
        duration = sf.info(str(path)).duration
    except Exception:
        return None

    if duration is None or duration < 0:
        return None
    if duration > MAX_SANE_CHUNK_MINUTES * 60:
        return None  # مدة فلكية = بيانات تالفة في الـ header، مش رقم حقيقي
    return duration


def audio_duration_minutes_safe(path: Path) -> float | None:
    """نفس فكرة _audio_duration_seconds بس بالدقايق ومتاحة برّه الموديول -
    لازم تتستخدم في أي مكان بيحسب مدة ملف صوت مباشرة (بدل استدعاء
    sf.info().duration مباشرة زي ما كان بيحصل في _start_processing)، عشان
    الـ sanity check ضد الملفات التالفة يتطبق في كل مكان مش في list_lecture_chunks بس."""
    seconds = _audio_duration_seconds(path)
    return None if seconds is None else seconds / 60


def list_lecture_chunks(lecture: str, state: dict, auto_repair: bool = True) -> list[dict]:
    """
    يرجع كل جزء صوتي للمحاضرة دي، مع حالته وطوله وحجمه. لو نفس الجزء
    (نفس الـ timestamp) موجود بصيغتين (FLAC وOpus) بسبب باج قديم، بياخد
    الـ Opus بس ويتجاهل الـ FLAC المكرر عشان يمنع التفريغ المزدوج.

    لو auto_repair=True ولقى ملف FLAC تالف (غالباً ناتج عن قفل مفاجئ للتسجيل
    بحيث duration غير صالحة)، بيحاول يصلحه تلقائياً عبر repair_corrupted_audio().

    "corrupted": True معناها المدة اللي جاية من الملف نفسه غير منطقية
    (غالبًا الملف اتقفل بشكل غير سليم) - الأجزاء دي بيتم استبعادها من أي
    إجمالي (مدة/تقدير توكينز) في الواجهة، وميتحطش تحديدها تلقائي.
    """
    # فحص وإصلاح استباقي للملفات التالفة إن وُجدت
    if auto_repair and ffmpeg_available():
        for flac_file in _glob_lecture_audio(lecture, "flac"):
            # لو مدته غير صالحة، نحاول نصلحه فوراً
            if _audio_duration_seconds(flac_file) is None:
                repair_corrupted_audio(flac_file)

    by_stem_prefix = {}  # timestamp -> path (بالأولوية لـ opus)
    for ext, priority in (("opus", 0), ("flac", 1), ("wav", 2)):
        for p in _glob_lecture_audio(lecture, ext):
            ts = p.stem.rsplit("__", 1)[-1]
            if ts not in by_stem_prefix or priority < by_stem_prefix[ts][1]:
                by_stem_prefix[ts] = (p, priority)

    chunks = []
    for ts, (path, _prio) in sorted(by_stem_prefix.items()):
        if path.name in state.get("explained_files", []):
            status = "explained"
        elif path.name in state.get("transcribed_files", []):
            status = "transcribed"
        else:
            status = "recorded"

        duration = _audio_duration_seconds(path)
        if duration is None and path.suffix == ".opus":
            # الملف .opus ده ممكن يكون لسه بيتضغط فعليًا (ffmpeg بيكتبه
            # تدريجيًا) - مش تالف (Bug #02 في تقرير المراجعة). الدليل
            # الأضمن مش توقيت (مفيش رقم ثواني تخميني ممكن يغلط)، لكن
            # حقيقة إن compress_to_opus() بيسيب الـ FLAC الأصلي موجود
            # جنب الـ Opus طول ما الضغط شغال، ومابيمسحوش غير بعد ما
            # يتأكد إن الناتج كامل وسليم. لو الـ FLAC ده لسه موجود وقابل
            # للقراءة، نستخدم مدته (الملف المضمون الكامل) بدل ما نعتبر
            # الجزء كله تالف. لو مفيش FLAC أصلًا (الحالة العادية بعد ما
            # الضغط يخلص بنجاح)، فعلاً نعتبره تالف زي ما كان قبل كده.
            sibling_flac = path.with_suffix(".flac")
            if sibling_flac.exists():
                flac_duration = _audio_duration_seconds(sibling_flac)
                if flac_duration is not None:
                    duration = flac_duration

        chunks.append({
            "filename": path.name,
            "path": path,
            "timestamp": ts,
            "duration_sec": duration if duration is not None else 0.0,
            "corrupted": duration is None,
            "size_mb": round(path.stat().st_size / (1024 * 1024), 2),
            "status": status,
        })
    return chunks


def pending_summary(lecture: str, state: dict) -> dict:
    """إجمالي مدة/حجم الأجزاء اللي لسه ماتفرغتش، لعرض تنبيه لو تراكم كتير.
    الأجزاء التالفة (corrupted) بتتستبعد من الإجمالي بالكامل - جمع مدة
    تالفة مع مدد حقيقية بيبوظ الرقم النهائي كله، فبنعرضها بشكل منفصل."""
    chunks = list_lecture_chunks(lecture, state)
    pending = [c for c in chunks if c["status"] == "recorded" and not c["corrupted"]]
    corrupted_pending = [c for c in chunks if c["status"] == "recorded" and c["corrupted"]]
    return {
        "count": len(pending),
        "total_minutes": round(sum(c["duration_sec"] for c in pending) / 60, 1),
        "total_mb": round(sum(c["size_mb"] for c in pending), 1),
        "corrupted_count": len(corrupted_pending),
    }


def delete_audio_file(path: Path) -> bool:
    """يمسح ملف صوتي بأمان (يُستخدم اختيارياً بعد نجاح التفريغ)."""
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except Exception:
        return False


def delete_lecture_data(
    lecture: str,
    delete_audio: bool = False,
    delete_transcript: bool = False,
    delete_notes: bool = False,
) -> dict:
    """
    مسح انتقائي لبيانات محاضرة/جلسة حسب اختيار المستخدم:
    - delete_audio: كل ملفات الصوت (opus/flac/wav) الخاصة بالمحاضرة دي
    - delete_transcript: ملف الترانسكريبت الخام (.txt)
    - delete_notes: ملف النوتس (.md)

    وبيظبط الـ state تبعًا لكل اختيار عشان لو المستخدم كمّل تفريغ/تلخيص
    بعد المسح، البرنامج يتصرف صح (مايفتكرش حاجة اتعملت وهي فعليًا اتمسحت):

    - مسح الترانسكريبت: أي جزء صوت اتفرّغ بس لسه ما اتحوّلش لنوتس بيرجع
      لحالة "متسجل بس" (عشان النص الخام بتاعه راح فعلاً)، أما الأجزاء
      اللي بالفعل ليها نوتس مستخرجة (explained_files) فبتفضل زي ما هي.
      summarized_chars بيترجع صفر عشان يتزامن مع الملف الجديد اللي هيتفتح.
    - مسح النوتس: كل الأجزاء المتفرّغة بترجع لحالة "متفرّغ" (عشان تقدر
      تحوّلها لنوتس تاني)، وبيترمى أي نسخة احتياطية للتراجع (_undo) لأنها
      بقت مبنية على نوتس متمسوحة.

    بيرجع تقرير بعدد/نوع الملفات اللي اتمسحت فعليًا.
    """
    report = {"audio_files": 0, "transcript": False, "notes": False}

    if delete_audio:
        for ext in ("opus", "flac", "wav"):
            for p in _glob_lecture_audio(lecture, ext):
                try:
                    p.unlink(missing_ok=True)
                    report["audio_files"] += 1
                except Exception:
                    pass

    if delete_transcript or delete_notes:
        state = load_state(lecture)

        if delete_transcript:
            transcript_path = TRANSCRIPT_FOLDER / f"{lecture}.txt"
            if transcript_path.exists():
                transcript_path.unlink(missing_ok=True)
                report["transcript"] = True
            state["transcribed_files"] = [
                fn for fn in state["transcribed_files"] if fn in state["explained_files"]
            ]
            state["summarized_chars"] = 0
            state["transcript_ranges"] = {}

        if delete_notes:
            md_path = MARKDOWN_FOLDER / f"{lecture}.md"
            if md_path.exists():
                md_path.unlink(missing_ok=True)
                report["notes"] = True
            state["explained_files"] = []
            state["summarized_chars"] = 0
            state.pop("_undo_stack", None)
            state.pop("_undo", None)  # اسم قديم لباج قديم، احتياطي لو لسه موجود

        with get_lecture_lock(lecture):
            save_state(lecture, state)

    return report


def delete_specific_files(
    lecture: str,
    audio_filenames: list[str] | None = None,
    transcript_filenames: list[str] | None = None,
) -> dict:
    """
    مسح انتقائي على مستوى الملف الواحد بدل النوع كله:

    - audio_filenames: أسماء ملفات صوت بعينها تتمسح من على الديسك (أي
      حالة: متسجل/متفرّغ/متشرّح - مش مربوطة بحالة التفريغ).
    - transcript_filenames: أسماء ملفات صوت (اللي بالفعل "متفرّغة" بس
      لسه "مش متشرّحة") نمسح تفريغها بس من ملف الترانسكريبت التراكمي،
      من غير ما نلمس باقي التفريغ. الملفات "المتشرّحة" (موجودة في
      explained_files) مينفعش تفريغها يتمسح لوحده هنا، لأن النوتس بالفعل
      اتبنت على النص ده - لازم تُمسح كل التفريغ (delete_lecture_data)
      لو عايز تشيلها فعلاً.

    بيرجع تقرير بعدد/أسامي اللي فعلاً اتمسحوا وأي أسامي اتجاهلت وليه.
    """
    audio_filenames = audio_filenames or []
    transcript_filenames = transcript_filenames or []
    report = {
        "audio_deleted": [],
        "transcript_deleted": [],
        "transcript_skipped_explained": [],
    }

    for fn in audio_filenames:
        p = RECORD_FOLDER / fn
        try:
            if p.exists():
                p.unlink()
                report["audio_deleted"].append(fn)
        except Exception:
            pass

    if transcript_filenames:
        with get_lecture_lock(lecture):
            state = load_state(lecture)
            ranges = state.get("transcript_ranges", {})
            transcript_path = TRANSCRIPT_FOLDER / f"{lecture}.txt"

            # نستبعد أي ملف اتشرح بالفعل - ماينفعش يتمسح تفريغه لوحده
            to_remove = []
            for fn in transcript_filenames:
                if fn in state.get("explained_files", []):
                    report["transcript_skipped_explained"].append(fn)
                    continue
                if fn in ranges:
                    to_remove.append(fn)

            if to_remove and transcript_path.exists():
                with open(transcript_path, "r", encoding="utf-8") as f:
                    full_text = f.read()

                # نمسح من الآخر للأول (بترتيب الـ start تنازلي) عشان مسح
                # جزء مايبوظش الـ offsets بتاعة الأجزاء اللي قبله
                to_remove.sort(key=lambda fn: ranges[fn][0], reverse=True)

                for fn in to_remove:
                    start, end = ranges[fn]
                    start = max(0, min(start, len(full_text)))
                    end = max(start, min(end, len(full_text)))

                    # أمان: لو الجزء ده بعد آخر حاجة اتشرحت أصلاً (وهو
                    # المفروض دايماً كده لأنه لسه مش متشرّح)، مالوش تأثير
                    # على summarized_chars. لو حصل تداخل غريب (بيانات
                    # قديمة)، نقلل summarized_chars عشان نفضل في الأمان.
                    removed_len = end - start
                    if state.get("summarized_chars", 0) > start:
                        state["summarized_chars"] = max(
                            0, state["summarized_chars"] - removed_len
                        )

                    full_text = full_text[:start] + full_text[end:]

                    # نزود كل الـ ranges اللي بعد الجزء المتمسوح للشمال
                    for other_fn, (o_start, o_end) in ranges.items():
                        if other_fn != fn and o_start >= end:
                            ranges[other_fn] = [o_start - removed_len, o_end - removed_len]

                    ranges.pop(fn, None)
                    if fn in state.get("transcribed_files", []):
                        state["transcribed_files"].remove(fn)
                    report["transcript_deleted"].append(fn)

                with open(transcript_path, "w", encoding="utf-8") as f:
                    f.write(full_text)

                state["transcript_ranges"] = ranges
                save_state(lecture, state)

    return report


def delete_lecture_completely(lecture: str) -> dict:
    """مسح نهائي وكامل للمحاضرة/الجلسة - مختلف عن delete_lecture_data:
    ده بيمسح كل حاجة (صوت + تفريغ + نوتس) **وكمان ملف الـ state نفسه**
    (.state/<lecture>.json)، وهو ده اللي list_existing_lectures() بيعتمد
    عليه (مع أي صوت متبقي) عشان يعرض المحاضرة في القائمة من الأساس.

    من غير مسح ملف الـ state، محاضرة اتمسح كل محتواها (بـ
    delete_lecture_data بكل الأعلام True) كانت بتفضل ظاهرة في القائمة
    فاضية من غير أي سبب - المحاضرة دي كانت بتفضل "موجودة" شكليًا بس من
    غير أي بيانات فعلية تحتها.

    بيرجع تقرير بعدد/نوع الملفات اللي اتمسحت فعليًا."""
    report = {"audio_files": 0, "transcript": False, "notes": False, "state": False}

    if lecture in _RESERVED_STATE_FILENAMES:
        # list_existing_lectures() بتستبعد الأسماء دي من قايمة المحاضرات
        # أصلًا، لكن الدالة دي ممكن تتنادى مباشرة (برمجيًا) باسم محاضرة
        # من غير ما يمر على list_existing_lectures() الأول - فلازم فحص
        # مستقل هنا كمان. من غيره، نداء بـ "pending_feedback" كان ممكن
        # يمسح طابور التقييمات اللي لسه ما اتبعتتش نهائيًا (BUG-07 في
        # التقرير الجنائي)، رغم إنه مش محاضرة خالص.
        return report

    for ext in ("opus", "flac", "wav"):
        for p in _glob_lecture_audio(lecture, ext):
            try:
                p.unlink(missing_ok=True)
                report["audio_files"] += 1
            except Exception:
                pass

    transcript_path = TRANSCRIPT_FOLDER / f"{lecture}.txt"
    if transcript_path.exists():
        transcript_path.unlink(missing_ok=True)
        report["transcript"] = True

    md_path = MARKDOWN_FOLDER / f"{lecture}.md"
    if md_path.exists():
        md_path.unlink(missing_ok=True)
        report["notes"] = True

    with get_lecture_lock(lecture):
        state_path = STATE_FOLDER / f"{lecture}.json"
        if state_path.exists():
            state_path.unlink(missing_ok=True)
            report["state"] = True

    return report


# =========================================================================
# إعادة تسمية محاضرة: اسم المحاضرة متكرر كمفتاح في أماكن كتير (بادئة
# ملفات الصوت، اسم ملف الترانسكريبت، اسم ملف النوتس، اسم ملف الـ state،
# ومفاتيح جوه الـ state نفسه)، فمينفعش نعمل os.rename على مكان واحد بس.
# =========================================================================

class RenameError(Exception):
    """فشل إعادة تسمية المحاضرة (رسالة الخطأ مكتوبة بشكل مفهوم لليوزر)."""


def _rename_lecture_files(from_name: str, to_name: str) -> dict:
    """
    خطوة فعلية واحدة: بتسمي كل ملفات محاضرة واحدة (صوت + ترانسكريبت +
    نوتس) من from_name لـ to_name على الديسك بس - من غير ما تلمس ملف
    الـ state أو محتوى الملفات.

    بتبني خطة الـ rename كاملة الأول (كل الأزواج)، وبعدين تنفّذها خطوة
    خطوة. لو أي خطوة فشلت في المنتصف (مثلاً ملف مقفول من برنامج تاني)،
    بترجّع كل اللي اتسمّى قبلها بالعكس فورًا وترمي RenameError - عشان
    المحاضرة تفضل دايمًا إما بالاسم القديم بالكامل أو الجديد بالكامل،
    مفيش حالة وسط.
    """
    plan: list[tuple[Path, Path]] = []
    audio_count = 0
    for ext in ("opus", "flac", "wav"):
        for old_path in _glob_lecture_audio(from_name, ext):
            rest = old_path.name[len(f"{from_name}__"):]
            plan.append((old_path, RECORD_FOLDER / f"{to_name}__{rest}"))
            audio_count += 1

    had_transcript = (TRANSCRIPT_FOLDER / f"{from_name}.txt").exists()
    if had_transcript:
        plan.append((
            TRANSCRIPT_FOLDER / f"{from_name}.txt",
            TRANSCRIPT_FOLDER / f"{to_name}.txt",
        ))

    had_notes = (MARKDOWN_FOLDER / f"{from_name}.md").exists()
    if had_notes:
        plan.append((
            MARKDOWN_FOLDER / f"{from_name}.md",
            MARKDOWN_FOLDER / f"{to_name}.md",
        ))

    done: list[tuple[Path, Path]] = []
    try:
        for src, dst in plan:
            os.rename(src, dst)
            done.append((src, dst))
    except OSError as e:
        for src, dst in reversed(done):
            try:
                os.rename(dst, src)
            except OSError:
                pass  # أفضل مجهود - لو فشل الرجوع كمان، على الأقل مفيش استثناء تاني يبوّظ رسالة الخطأ الأصلية
        raise RenameError(
            f"فشلت إعادة التسمية في المنتصف ({e}) - المحاضرة رجعت لاسمها "
            "القديم زي ما كانت. غالبًا فيه ملف مفتوح/مقفول من برنامج تاني "
            "(مشغل صوت، مستكشف الملفات...)، اقفله وجرب تاني."
        ) from e

    return {"audio_files": audio_count, "transcript": had_transcript, "notes": had_notes}


def rename_lecture(old_lecture: str, new_lecture: str) -> dict:
    """
    إعادة تسمية محاضرة بالكامل: كل ملفات الصوت والترانسكريبت والنوتس
    وملف الـ state، بالإضافة لتحديث أي إشارة لاسم الملفات القديمة جوه
    الـ state نفسه (transcribed_files / explained_files /
    transcript_ranges) وسطر العنوان الأول جوه ملف الـ Markdown
    ("# {lecture}") - العنوان ده مش تفصيل بصري بس، process_lecture.py
    بيقارنه حرفيًا (undo_last_notes_update) عشان يقرر هل يمسح الملف كله
    لو فضل فاضي غير العنوان - لو فضل بالاسم القديم بعد الـ rename،
    المقارنة دي هتفشل بصمت والتراجع هيتصرف غلط.

    بترمي RenameError برسالة واضحة في أي حالة تعارض (اسم فاضي، نفس
    الاسم القديم، اسم محجوز داخليًا، اسم موجود بالفعل لمحاضرة تانية،
    أو فيه عملية شغالة على المحاضرة دي دلوقتي).

    بترجع تقرير: {"audio_files": n, "transcript": bool, "notes": bool,
    "new_name": الاسم النهائي بعد safe_name()}.
    """
    new_clean = safe_name(new_lecture)
    if not new_clean:
        raise RenameError("الاسم الجديد لازم يكون فيه حروف/أرقام صالحة.")
    if new_clean == old_lecture:
        raise RenameError("الاسم الجديد هو نفس الاسم القديم بالظبط.")
    if new_clean in _RESERVED_STATE_FILENAMES:
        raise RenameError(f"الاسم \"{new_clean}\" محجوز داخليًا - اختار اسم تاني.")

    # تغيير الحالة (Case) بس، زي "lecture" -> "Lecture": نظام ملفات
    # ويندوز case-insensitive، فـ "الاسم موجود بالفعل" هتتوهم إنه نفس
    # الاسم القديم، و os.rename مباشر بينهم ممكن "ميعملش حاجة" فعليًا.
    case_only = new_clean.lower() == old_lecture.lower()
    if not case_only and new_clean in list_existing_lectures():
        raise RenameError(f"فيه محاضرة بالاسم ده (\"{new_clean}\") موجودة بالفعل.")

    lock = get_lecture_lock(old_lecture)
    if not lock.acquire(blocking=False):
        raise RenameError(
            "فيه عملية (تسجيل/تفريغ/تلخيص) شغالة على المحاضرة دي دلوقتي "
            "- استنى تخلص وجرب تاني."
        )

    try:
        if case_only:
            # بنعدي على اسم وسيط مختلف تمامًا عن الاسمين الأول (مش مجرد
            # اختلاف حالة) عشان نضمن الفايل سيستم يعامله كـ rename حقيقي
            # في الخطوتين، مش "نفس الاسم" في أي منهم.
            tmp_name = f"{old_lecture}__rename_tmp__{int(time.time())}"
            _rename_lecture_files(old_lecture, tmp_name)
            try:
                report = _rename_lecture_files(tmp_name, new_clean)
            except RenameError:
                # رجّعنا للاسم الأصلي بالكامل (مش الوسيط) لو المرحلة
                # التانية فشلت - عشان مايفضلش اسم وسيط غريب ظاهر لليوزر
                _rename_lecture_files(tmp_name, old_lecture)
                raise
        else:
            report = _rename_lecture_files(old_lecture, new_clean)

        md_path = MARKDOWN_FOLDER / f"{new_clean}.md"
        if md_path.exists():
            try:
                content = md_path.read_text(encoding="utf-8")
                old_header = f"# {old_lecture}"
                if content.startswith(old_header):
                    content = f"# {new_clean}" + content[len(old_header):]
                    md_path.write_text(content, encoding="utf-8")
            except OSError:
                # فشل تحديث العنوان مش لازم يوقف الـ rename كله - الملفات
                # اتسمّت صح، العنوان جوه المحتوى تفصيل ثانوي بالمقارنة
                pass

        state = load_state(old_lecture)
        old_prefix = f"{old_lecture}__"
        new_prefix = f"{new_clean}__"

        def _renamed(name: str) -> str:
            return new_prefix + name[len(old_prefix):] if name.startswith(old_prefix) else name

        state["transcribed_files"] = [_renamed(n) for n in state.get("transcribed_files", [])]
        state["explained_files"] = [_renamed(n) for n in state.get("explained_files", [])]
        state["transcript_ranges"] = {
            _renamed(k): v for k, v in state.get("transcript_ranges", {}).items()
        }

        save_state(new_clean, state)
        (STATE_FOLDER / f"{old_lecture}.json").unlink(missing_ok=True)

        report["new_name"] = new_clean
        return report
    finally:
        lock.release()