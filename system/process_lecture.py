"""
يفرّغ ملفات صوت لمحاضرة معينة (Groq الأول للسرعة، Gemini كبديل)، يضيفها
لملف الترانسكريبت التراكمي (بيتحفظ دايماً كنسخة احتياطية خام)، وبعدين
يحوّل الجزء الجديد لنوتس مركزة بأسلوب المحاضر (Gemini الأول، Groq كبديل).

تشغيل: python process_lecture.py "اسم المحاضرة"
"""

import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from google import genai

from state_manager import (
    RECORD_FOLDER,
    TRANSCRIPT_FOLDER,
    MARKDOWN_FOLDER,
    load_state,
    save_state,
    delete_audio_file,
    get_lecture_lock,
    audio_duration_minutes_safe,
)

load_dotenv()

_log_callback = print
_progress_callback = None
_cancel_event = None  # threading.Event بيتحط من الواجهة عن طريق set_cancel_event


def set_logger(callback):
    global _log_callback
    _log_callback = callback


def set_progress_callback(callback):
    global _progress_callback
    _progress_callback = callback


def set_cancel_event(event) -> None:
    """يربط threading.Event من الواجهة، بيتفحص بين كل ملف/مقطع في اللوبات
    الطويلة (تفريغ/شرح) عشان نقدر نوقف العملية فعليًا نص الطريق من غير ما
    نستنى كل الأجزاء تخلص أو نضطر نقفل البرنامج بالغلط."""
    global _cancel_event
    _cancel_event = event


def _cancelled() -> bool:
    return _cancel_event is not None and _cancel_event.is_set()


def _log(msg: str):
    _log_callback(msg)


def _progress(done: int, total: int, label: str = ""):
    if _progress_callback:
        _progress_callback(done, total, label)


# =========================================================================
# رسائل أخطاء مبسّطة: الـ SDKs (google-genai / groq) بترجع exceptions
# بتفاصيل تقنية خام (status code، JSON body كامل أحياناً) - مش مفيد
# لمستخدم عادي واقف قدام log box. الدالة دي بتحاول تترجم أكتر الأخطاء
# شيوعًا لرسالة عربي مفهومة، وبترجع النص الخام زي ما هو لو محتش تعرفه.
# =========================================================================
_ERROR_PATTERNS = [
    (("api_key_invalid", "invalid api key", "api key not valid", "401"),
     "مفتاح الـ API غلط أو مش صالح - راجع ملف .env"),
    (("quota", "rate limit", "429", "resource_exhausted"),
     "تجاوزت الحد المسموح به من الطلبات (Rate limit/Quota) - جرب تاني بعد شوية"),
    (("permission_denied", "403"),
     "مفتاح الـ API مالوش صلاحية للموديل ده"),
    (("timeout", "timed out", "deadline"),
     "الاتصال بالسيرفر خد وقت أطول من اللازم (Timeout) - جرب تاني"),
    (("connection", "network", "getaddrinfo", "failed to resolve"),
     "مشكلة في الاتصال بالإنترنت"),
    (("file too large", "payload too large", "413"),
     "حجم الملف أكبر من الحد المسموح به عند المزوّد"),
]


def friendly_error(e: Exception) -> str:
    """يرجع نسخة مبسّطة من رسالة الخطأ لو عرف يتعرف عليها، وإلا بيرجع
    النص الخام زي ما هو (احتياطي، أفضل من ما نخفي معلومة قد تفيد)."""
    raw = str(e)
    low = raw.lower()
    for keywords, friendly in _ERROR_PATTERNS:
        if any(k in low for k in keywords):
            return f"{friendly}  (التفاصيل الخام: {raw[:200]})"
    return raw


# ------------------ إعدادات المزوّدين ------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-3.6-flash"

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_TRANSCRIBE_MODEL = "whisper-large-v3"
GROQ_TEXT_MODEL = "openai/gpt-oss-120b"

# ------------------ كتالوج الموديلات المتاحة للاختيار اليدوي ------------------
# كل قاموس هنا بيمثّل قائمة اختيار واحدة في الواجهة (زرار/dropdown واحد).
# المفتاح "auto" دايمًا الافتراضي، وبيمثّل نفس سلسلة المحاولة القديمة
# (Groq دقيق ← Gemini للتفريغ، Gemini ← Groq للتلخيص) من غير أي تغيير
# في السلوك الافتراضي لو المستخدم مغيّرش حاجة.
#
# القيمة التانية في الـ tuple: (provider, model_id) أو None لو "auto".
TRANSCRIBE_MODEL_CHOICES = {
    "auto":       ("🔄 Auto (Groq accurate → Groq fast → Gemini)", None),
    "groq_v3":    ("Groq - Accurate (whisper-large-v3)", ("groq", "whisper-large-v3")),
    "groq_turbo": ("Groq - Fast (whisper-large-v3-turbo)", ("groq", "whisper-large-v3-turbo")),
    "gemini":     ("Gemini", ("gemini", None)),
}

SUMMARY_MODEL_CHOICES = {
    "auto":            ("🔄 Auto (Gemini → Groq)", None),
    "gemini_flash":    ("Gemini - gemini-3.6-flash (balanced)", ("gemini", "gemini-3.6-flash")),
    "gemini_lite":     ("Gemini - gemini-3.5-flash-lite (fastest)", ("gemini", "gemini-3.5-flash-lite")),
    "gemini_37":       ("Gemini - gemini-3.7-flash (strongest Gemini)", ("gemini", "gemini-3.7-flash")),
    "groq_gptoss120b": ("Groq - gpt-oss-120b (strongest)", ("groq", "openai/gpt-oss-120b")),
    "groq_gptoss20b":  ("Groq - gpt-oss-20b (fastest)", ("groq", "openai/gpt-oss-20b")),
    "groq_qwen36":     ("Groq - qwen3.6-27b", ("groq", "qwen/qwen3.6-27b")),
}

TRANSCRIBE_MODEL_CHOICE = os.environ.get("TRANSCRIBE_MODEL_CHOICE", "auto")
if TRANSCRIBE_MODEL_CHOICE not in TRANSCRIBE_MODEL_CHOICES:
    TRANSCRIBE_MODEL_CHOICE = "auto"

SUMMARY_MODEL_CHOICE = os.environ.get("SUMMARY_MODEL_CHOICE", "auto")
if SUMMARY_MODEL_CHOICE not in SUMMARY_MODEL_CHOICES:
    SUMMARY_MODEL_CHOICE = "auto"


def _save_choice_to_env(key_name: str, value: str) -> None:
    """يحدّث سطر واحد بس في .env من غير ما يلمس باقي المحتوى - نفس
    الأسلوب المستخدم في first_run_setup._save_env، هنا بس لتفضيلات
    اختيار الموديل."""
    env_path = Path(__file__).resolve().parent / ".env"
    lines = []
    if env_path.exists():
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []

    prefix = f"{key_name}="
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[i] = f"{key_name}={value}"
            break
    else:
        lines.append(f"{key_name}={value}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_transcribe_model_choice(choice_key: str) -> None:
    """بيتنادى من الواجهة لما المستخدم يغيّر اختيار موديل التفريغ - بيتفعّل
    فورًا (من غير restart) وبيتحفظ في .env عشان يفضل زي ما اختاره."""
    global TRANSCRIBE_MODEL_CHOICE
    if choice_key not in TRANSCRIBE_MODEL_CHOICES:
        return
    TRANSCRIBE_MODEL_CHOICE = choice_key
    os.environ["TRANSCRIBE_MODEL_CHOICE"] = choice_key
    _save_choice_to_env("TRANSCRIBE_MODEL_CHOICE", choice_key)


def set_summary_model_choice(choice_key: str) -> None:
    """نفس فكرة set_transcribe_model_choice، لاختيار موديل التلخيص/النوتس."""
    global SUMMARY_MODEL_CHOICE
    if choice_key not in SUMMARY_MODEL_CHOICES:
        return
    SUMMARY_MODEL_CHOICE = choice_key
    os.environ["SUMMARY_MODEL_CHOICE"] = choice_key
    _save_choice_to_env("SUMMARY_MODEL_CHOICE", choice_key)


def _run_with_timeout(target_callable, timeout_sec: float):
    """
    بيشغّل target_callable() في ثريد جانبي (daemon) ولو ماخلصش قبل
    timeout_sec، بيرجع ("timeout", None) على طول من غير ما يستنى - الثريد
    الأصلي بيكمل شغل في الخلفية لحد ما يخلص أو يفشل، بس بيتجاهل ناتجه
    (مفيش طريقة نضيفة نقفل استدعاء شبكة شغال فعليًا في بايثون، فأحسن حل
    عملي إننا نسيبه وناخد قرار fallback فورًا بدل ما نستناه).
    """
    result_box = {}

    def _wrap():
        try:
            result_box["value"] = target_callable()
        except Exception as e:
            result_box["error"] = e

    t = threading.Thread(target=_wrap, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive():
        return "timeout", None
    if "error" in result_box:
        return "error", result_box["error"]
    return "ok", result_box.get("value")

MAX_CHARS_PER_CHUNK = 15000

# حد حجم الملف المسموح بيه على خطة Groq المجانية (25MB بدون تقطيع الصوت نفسه).
# لو الجزء أكبر من كده (مثلاً لو ffmpeg مش متثبت وفضل الملف FLAC خام)، نتخطى
# محاولة Groq تمامًا ونروح مباشرة لـ Gemini بدل ما نستنى فشل مؤكد.
GROQ_MAX_FILE_BYTES = 25 * 1024 * 1024

TRANSCRIBE_PROMPT = """فرّغ هذا التسجيل الصوتي إلى نص عربي كامل وحرفي (اكتب اللهجة
كما نُطقت، بدون ترجمتها للفصحى وبدون تلخيص). اكتب كل ما قيل بالترتيب،
بدون أي تعليق أو مقدمة منك، فقط النص المفرغ نفسه."""

# البرومبت ده مصمم لهدف "Note Taker": ياخد كلام المحاضر ويحوّله لنوتس
# مركزة ومنظمة (مش شرح مطوّل)، مع إبراز أي نقطة أكد عليها المحاضر نفسه
EXPLAIN_PROMPT = """أنت مساعد تدوين ملاحظات أكاديمي (Note Taker) متخصص في
المحتوى التقني (برمجة، AI، هندسة حاسبات). هتستلم جزء من نص مفرغ من محاضرة
صوتية بصوت المحاضر (Instructor)، ممكن فيه أخطاء بسيطة من التفريغ الآلي،
وممكن يكون بلهجة مصرية.

هدفك: تحوّل كلام المحاضر لنوتس مركزة ومنظمة يقدر الطالب يراجع بيها بسرعة،
مش مقال طويل. اتبع القواعد دي بالظبط:

1. حدّد كل نقطة/فكرة قالها المحاضر، واكتبها كعنوان فرعي قصير (### العنوان).
2. تحت كل عنوان، اكتب نقاط (bullet points) مختصرة ومباشرة تلخص اللي
   المحاضر قاله بالظبط - مش تشرح بإسهاب، خد جوهر الكلام وبس.
3. لو نقطة محتاجة توضيح إضافي عشان تفهم (مصطلح، معادلة، خطوة في خوارزمية)،
   ضيف سطر توضيح قصير بعدها، أو مثال عملي مختصر جداً (كود أو رقم) لو ده
   هيوضح الفكرة بسرعة أكتر من الكلام.
4. اكتب المصطلحات التقنية بالإنجليزي زي ما هي، والباقي عربي فصحى واضح.

**الأهم: لازم تبرز أي حاجة المحاضر شدّد عليها أو كررها أو نبّه عليها أو
طلبها من اللي بيتفرج**، باستخدام الصناديق دي بالظبط (Markdown blockquote) -
كل حالة ليها إيموجي ولون مختلف عشان الطالب يقدر يميّز بسرعة وهو بيراجع:

- تأكيد على نقطة مهمة (زي "ده مهم جداً"، أو كرر نفس الفكرة أكتر من مرة):
  > 💡 **مهم:** [النقطة اللي أكد عليها]

- سؤال إنترفيو محتمل (زي "هيسألوك في الإنترفيو عن كذا"، أو "دي حاجة بتتسأل
  كتير"، أو "لازم تعرفها للإنترفيوهات"):
  > 🎯 **سؤال إنترفيو محتمل:** [السؤال أو النقطة]

- تحذير أو خطأ شائع أو حاجة الطلبة بينسوها:
  > ⚠️ **تنبيه:** [النقطة]

- **مهمة/تاسك مطلوب تنفيذه** - أي حاجة المحاضر طلب من اللي بيتفرج ينفذها
  بنفسه (زي "جرب كذا وطبقه"، "دي تاسك ليكم"، "هوم ورك"، "قبل الحصة الجاية
  عايزكم تعملوا كذا"، "وقف الفيديو وحل كذا لوحدك"، "ابحث عن كذا وارجعلي"):
  > ✅ **تاسك/مطلوب تنفيذه:** [المهمة بالظبط زي ما طلبها المحاضر]

- مرجع أو مصدر خارجي نصح بيه المحاضر (كتاب، لينك، بيبر، توثيق رسمي،
  فيديو تاني، قناة يوصي بمتابعتها):
  > 📚 **مصدر إضافي:** [اسم المصدر/الموضوع اللي يتراجع منه]

- تعريف رسمي لمصطلح جديد قدّمه المحاضر لأول مرة بشكل واضح ("كذا معناه
  كذا"، "التعريف الرسمي لـ..."):
  > 📌 **تعريف:** [المصطلح: التعريف المختصر]

- خلاصة/تلخيص قاله المحاضر نفسه لجزء كامل ("يعني اللي احنا اتكلمنا عنه
  بيلخص في...", "خلاصة الكلام..."):
  > 🔁 **خلاصة:** [النقاط اللي لخصها]

- مثال تطبيقي أو تمرين حلّه المحاضر بالتفصيل خطوة بخطوة أثناء الشرح
  (مش مجرد مثال عابر - لما يبقى فعلاً بيحل حاجة كاملة قدام الطلبة):
  > 🧩 **مثال محلول:** [وصف مختصر للمثال والخطوات الأساسية]

- أي حاجة مرتبطة بميعاد أو امتحان أو كويز أو تسليم (deadline، تاريخ
  امتحان، آخر ميعاد تسليم مشروع):
  > 🕒 **ميعاد/امتحان:** [التفاصيل]

استخدم الصناديق دي بس لما فعلاً يكون فيه إشارة واضحة من المحاضر في الكلام
نفسه، متخترعش نقط مهمة من عندك، ومتحطش أكتر من صندوق واحد لنفس الجملة لو
مش محتاجة. خلي كل حاجة مختصرة ومركزة - الهدف نوتس للمراجعة السريعة، مش
توثيق شامل. متكتبش أي مقدمة أو خاتمة عامة، ادخل في النقط على طول."""

# البرومبت ده لوضع "خد نوتس" - محضر اجتماع مختصر وعملي، مختلف تمامًا عن
# أسلوب "لخص المحاضرة" التعليمي فوق. الهدف توثيق اللي اتقال/اتقرر، مش شرحه.
MEETING_NOTES_PROMPT = """أنت مساعد تدوين محضر اجتماعات (Meeting Minutes) محترف.
هدفك إنك تحوّل نص خام (تفريغ صوتي لاجتماع) لمحضر مختصر ومنظم، مش شرح
تعليمي - يعني ملخص عملي يقدر أي حد يقراه في دقيقتين ويعرف اتقال إيه
واتقرر إيه ومطلوب إيه من مين.

اكتب المحضر بالتنسيق ده بالظبط (Markdown)، واستبعد أي قسم مفيش له محتوى
فعلي في الكلام (متخترعش حاجة مش موجودة):

## 📋 ملخص الاجتماع
جملتين-تلاتة بيلخصوا موضوع الاجتماع العام وهدفه.

## 🗣️ أهم النقاط اللي اتناقشت
- نقطة مختصرة
- نقطة مختصرة
(bullet points قصيرة، مش فقرات - كل نقطة سطر واحد لو أمكن)

أثناء كتابة النقاط، لو جالك مصطلح تقني أو اختصار أو اسم أداة/مشروع مش
معروف للقارئ العادي، ضيف توضيح فوري جوه صندوق زي كده مباشرة بعد النقطة:
> 📌 **[المصطلح]:** [شرح في جملة واحدة قصيرة جدًا - مش فقرة]
استخدمه بس لو فعلاً غير واضح، متشرحش حاجات بديهية.

## ✅ القرارات والمهام (Action Items)
> ✅ **[اسم الشخص لو معروف، أو "غير محدد"]:** [المهمة المطلوبة بالظبط]
(كرر الصندوق ده لكل مهمة أو قرار اتاخد - ده أهم قسم في المحضر)

## 🕒 مواعيد ومهل
> 🕒 **[الميعاد/الديدلاين]:** [التفاصيل]
(لو مفيش مواعيد اتذكرت، احذف القسم ده تمامًا)

## ⚠️ تنبيهات ومخاطر
> ⚠️ **[التنبيه]:** [التفاصيل - أي خطر أو عائق أو مشكلة محتملة اتقالت بوضوح]
(لو مفيش، احذف القسم ده تمامًا)

## ❓ نقاط مفتوحة / محتاجة متابعة
- أي سؤال أو موضوع اتقال هيتراجع/يتقرر لاحقًا
(لو مفيش، احذف القسم ده تمامًا)

قواعد مهمة:
- ممنوع تضيف أي تحليل أو شرح تعليمي مطوّل - إنت بتوثّق اللي اتقال بس،
  والتوضيحات (📌) لازم تكون سطر واحد قصير جدًا، مش أكتر.
- لو الكلام مش فيه قرارات أو مهام واضحة خالص، سيب قسم "القرارات والمهام"
  فاضي وقول صراحة "مفيش قرارات أو مهام واضحة اتحددت في الجزء ده".
- خليك مختصر جدًا - لو النقطة ممكن تتقال في سطر، متطولش فيها.
- متكتبش أي مقدمة أو خاتمة عامة برة الأقسام دي، ادخل في المحضر على طول."""


# =========================================================================
# طبقة الاتصال بالنماذج
# =========================================================================

def _gemini_client() -> "genai.Client":
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY مش موجود")
    return genai.Client(api_key=GEMINI_API_KEY)


def _transcribe_with_gemini(audio_path, model: str = None) -> str:
    client = _gemini_client()
    uploaded = client.files.upload(file=str(audio_path))
    while uploaded.state.name == "PROCESSING":
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)
    if uploaded.state.name == "FAILED":
        raise RuntimeError("فشل رفع الملف الصوتي لـ Gemini")

    response = client.models.generate_content(
        model=model or GEMINI_MODEL,
        contents=[TRANSCRIBE_PROMPT, uploaded],
    )
    text = (response.text or "").strip()
    if not text:
        # response.text ممكن ترجع "" من غير أي استثناء (safety filter،
        # وصول لحد التوكينز، أو رد فاضي مؤقت من السيرفر) - لازم نعتبرها
        # فشل صريح عشان الكود يعمل fallback بدل ما يحفظ نتيجة فاضية بصمت.
        raise RuntimeError("Gemini رجّع نص تفريغ فاضي")
    return text


def _transcribe_with_groq(audio_path, model: str = None) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY مش موجود")
    from groq import Groq

    client = Groq(api_key=GROQ_API_KEY)
    with open(audio_path, "rb") as f:
        result = client.audio.transcriptions.create(
            file=(str(audio_path), f.read()),
            model=model or GROQ_TRANSCRIBE_MODEL,
            language="ar",
            response_format="text",
        )
    return str(result).strip()


def _transcribe_candidates() -> list:
    """بيرجع قائمة (provider, model_id) بالترتيب اللي هيتجرب بيه - المختار
    يدويًا (لو مش "auto") بييجي الأول، وبعده باقي السلسلة الافتراضية
    (من غير تكرار) كـ fallback لو المختار فشل/اتأخر."""
    default_chain = [("groq", "whisper-large-v3"), ("gemini", None)]
    choice = TRANSCRIBE_MODEL_CHOICE
    if choice == "auto" or choice not in TRANSCRIBE_MODEL_CHOICES:
        return default_chain

    chosen = TRANSCRIBE_MODEL_CHOICES[choice][1]
    rest = [c for c in default_chain if c != chosen]
    return [chosen] + rest


def transcribe_audio_file(audio_path) -> str:
    """
    بيجرب المرشحين بالترتيب اللي رجعه _transcribe_candidates() (المختار
    يدويًا الأول لو موجود، وبعده الافتراضي كـ fallback). لكل محاولة، لو
    اتأخرت عن وقت متوقع (تقريبي، مبني على مدة الصوت) بيتجاوزها للتالي
    فورًا من غير ما يستناها - مش بس لو فشلت بـ exception.
    """
    t0 = time.time()

    try:
        size_bytes = Path(audio_path).stat().st_size
    except Exception:
        size_bytes = 0

    duration_min = audio_duration_minutes_safe(audio_path)
    if not duration_min or duration_min <= 0:
        duration_min = 5.0  # قيمة احتياطية تقريبية لو فشلنا نقرأ مدة الملف
    # تقدير تقريبي جدًا: ~12 ثانية معالجة لكل دقيقة صوت، بحد أدنى 30 ثانية
    timeout_sec = max(30.0, duration_min * 12)

    candidates = _transcribe_candidates()
    errors = []

    for provider, model_id in candidates:
        if provider == "groq" and size_bytes > GROQ_MAX_FILE_BYTES:
            errors.append("Groq: الملف أكبر من حد الـ 25MB المسموح بيه")
            continue

        label = f"Groq ({model_id})" if provider == "groq" else "Gemini"
        _log(f"    → بيحاول عبر {label}...")
        fn = (lambda p=audio_path, m=model_id: _transcribe_with_groq(p, m)) if provider == "groq" \
            else (lambda p=audio_path: _transcribe_with_gemini(p))
        status, result = _run_with_timeout(fn, timeout_sec)

        if status == "ok":
            _log(f"    ✓ خلص عبر {label} ({time.time() - t0:.1f} ثانية)")
            return result
        if status == "timeout":
            _log(f"    ⚠ {label} اتأخر عن المتوقع (~{timeout_sec:.0f} ثانية) - بيجرب التالي...")
            errors.append(f"{label}: اتأخر عن المتوقع")
        else:
            _log(f"    ⚠ {label} فشل ({friendly_error(result)}), بيجرب التالي...")
            errors.append(f"{label}: {friendly_error(result)}")

    raise RuntimeError("فشل التفريغ في كل المحاولات:\n  " + "\n  ".join(errors))


def _explain_with_gemini(text: str, prompt: str = EXPLAIN_PROMPT, model: str = None) -> str:
    client = _gemini_client()
    response = client.models.generate_content(
        model=model or GEMINI_MODEL,
        contents=text,
        config={"system_instruction": prompt},
    )
    result = (response.text or "").strip()
    if not result:
        # نفس مبدأ التفريغ: رد فاضي من Gemini لازم يتعامل معاه كفشل صريح
        # (مش كنجاح بمحتوى فاضي) عشان الـ fallback يشتغل فعليًا.
        raise RuntimeError("Gemini رجّع نوتس فاضية")
    return result


def _explain_with_groq(text: str, prompt: str = EXPLAIN_PROMPT, model: str = None) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY مش موجود")
    from groq import Groq

    client = Groq(api_key=GROQ_API_KEY)
    completion = client.chat.completions.create(
        model=model or GROQ_TEXT_MODEL,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
    )
    result = (completion.choices[0].message.content or "").strip()
    if not result:
        raise RuntimeError("Groq رجّع نوتس فاضية")
    return result


def _summary_candidates() -> list:
    """نفس فكرة _transcribe_candidates بس لقائمة اختيار موديل التلخيص."""
    default_chain = [("gemini", None), ("groq", None)]
    choice = SUMMARY_MODEL_CHOICE
    if choice == "auto" or choice not in SUMMARY_MODEL_CHOICES:
        return default_chain

    chosen = SUMMARY_MODEL_CHOICES[choice][1]
    rest = [c for c in default_chain if c[0] != chosen[0]]
    return [chosen] + rest


def explain_text(text: str, prompt: str = EXPLAIN_PROMPT) -> str:
    """
    بيجرب المرشحين بالترتيب اللي رجعه _summary_candidates() (المختار
    يدويًا الأول لو موجود، وبعده الافتراضي Gemini←Groq كـ fallback).
    لكل محاولة، لو اتأخرت عن وقت متوقع (تقريبي، مبني على طول النص)
    بيتجاوزها للتالي فورًا من غير ما يستناها.
    """
    t0 = time.time()
    # تقدير تقريبي جدًا (مش دقيق): ~45 ثانية أساسية + وقت إضافي حسب طول
    # النص - مبني على ملاحظة عملية إن الأجزاء الطويلة بتاخد وقت أطول بكتير.
    timeout_sec = max(45.0, 45 + (len(text) / 1000) * 8)

    candidates = _summary_candidates()
    errors = []

    for provider, model_id in candidates:
        label = f"Groq ({model_id})" if (provider == "groq" and model_id) else (
            f"Gemini ({model_id})" if (provider == "gemini" and model_id) else provider.capitalize()
        )
        _log(f"    → بيحاول عبر {label} (ممكن ياخد وقت حسب طول الجزء)...")
        fn = (lambda p=prompt, m=model_id: _explain_with_groq(text, p, m)) if provider == "groq" \
            else (lambda p=prompt, m=model_id: _explain_with_gemini(text, p, m))
        status, result = _run_with_timeout(fn, timeout_sec)

        if status == "ok":
            _log(f"    ✓ خلص عبر {label} ({time.time() - t0:.1f} ثانية)")
            return result
        if status == "timeout":
            _log(f"    ⚠ {label} اتأخر عن المتوقع (~{timeout_sec:.0f} ثانية) - بيجرب التالي...")
            errors.append(f"{label}: اتأخر عن المتوقع")
        else:
            _log(f"    ⚠ {label} فشل ({friendly_error(result)}), بيجرب التالي...")
            errors.append(f"{label}: {friendly_error(result)}")

    raise RuntimeError("فشل الشرح في كل المحاولات:\n  " + "\n  ".join(errors))


# =========================================================================
# التفريغ
# =========================================================================

def transcribe_files(
    lecture: str, state: dict, files_to_process: list,
    delete_after_success: bool = False,
) -> str:
    """
    يفرّغ قائمة ملفات صوت محددة (files_to_process)، يضيفها لملف
    الترانسكريبت التراكمي (اللي بيفضل محفوظ دايماً كنسخة احتياطية خام
    بغض النظر عن التلخيص)، ويرجع النص الكامل المتراكم بعد الإضافة.
    """
    transcript_path = TRANSCRIPT_FOLDER / f"{lecture}.txt"

    if not files_to_process:
        _log("[i] مفيش ملفات محددة للتفريغ.")
    else:
        # حماية: أي ملف اتفرغ قبل كده (حتى لو اتحدد بالغلط من الواجهة)
        # يتجاهله تلقائياً، عشان مايحصلش تفريغ مزدوج لنفس المحتوى
        already_done = [p for p in files_to_process if p.name in state["transcribed_files"]]
        files_to_process = [p for p in files_to_process if p.name not in state["transcribed_files"]]
        for p in already_done:
            _log(f"[i] {p.name} اتفرغ قبل كده، هيتخطاه (مفيش تفريغ مزدوج).")

        total = len(files_to_process)
        if total == 0:
            _log("[i] كل الملفات المحددة اتفرغت بالفعل، مفيش حاجة جديدة تتفرغ.")

        # بنبدأ من الطول الحالي (بالحروف) لملف الترانسكريبت التراكمي، عشان
        # نسجل مكان (start/end) كل ملف جديد بالظبط جوه النص - ده اللي
        # بيسمح لاحقاً بمسح تفريغ ملف واحد بس من state_manager.delete_specific_files
        cursor = len(transcript_path.read_text(encoding="utf-8")) if transcript_path.exists() else 0
        ranges = state.setdefault("transcript_ranges", {})

        with open(transcript_path, "a", encoding="utf-8") as f:
            for i, audio_path in enumerate(files_to_process, 1):
                if _cancelled():
                    _log(f"[⏹] اتلغت العملية - اتفرّغ {i - 1}/{total} قبل الإلغاء، والباقي متجاهل.")
                    break

                _log(f"[+] بيرفع ويفرّغ ({i}/{total}): {audio_path.name}")
                _progress(i - 1, total, "تفريغ الصوت")
                try:
                    text = transcribe_audio_file(audio_path)
                    if text:
                        chunk = text + "\n\n"
                        f.write(chunk)
                        ranges[audio_path.name] = [cursor, cursor + len(chunk)]
                        cursor += len(chunk)

                    if audio_path.name not in state["transcribed_files"]:
                        state["transcribed_files"].append(audio_path.name)
                    with get_lecture_lock(lecture):
                        save_state(lecture, state)

                    if delete_after_success:
                        if delete_audio_file(audio_path):
                            _log(f"    🗑 اتمسح الصوت الأصلي: {audio_path.name}")

                except Exception as e:
                    _log(f"[!] تعذر تفريغ {audio_path.name}: {e}")
                    _log("    هيتجاهل الملف ده ويكمل اللي بعده.")
                    continue
                _progress(i, total, "تفريغ الصوت")

    if not transcript_path.exists():
        return ""
    with open(transcript_path, "r", encoding="utf-8") as f:
        return f.read()


def transcribe_new_files(lecture: str, state: dict, delete_after_success: bool = False) -> str:
    """يفرّغ كل الملفات اللي لسه ما اتفرغتش (الاستخدام الافتراضي)."""
    from state_manager import list_lecture_chunks
    chunks = list_lecture_chunks(lecture, state)
    pending = [c["path"] for c in chunks if c["status"] == "recorded"]
    return transcribe_files(lecture, state, pending, delete_after_success)


# =========================================================================
# الشرح/النوتس
# =========================================================================

# نهاية جملة = نقطة/علامة استفهام (عربي أو إنجليزي)/تعجب متبوعة بمسافة، أو سطر
# جديد. القطع هنا بيحافظ على حدود الجُمل بدل ما يقطع في نص فكرة أو كلمة.
_SENTENCE_END_RE = re.compile(r"(?<=[.!؟?])\s+|\n+")


def estimate_tokens_for_chars(char_count: int) -> int:
    """تقدير تقريبي وبسيط جداً لعدد التوكينز (مش دقيق، بس بيدي فكرة عامة
    قبل التنفيذ) - تقريبًا 4 حروف عربي/إنجليزي للتوكن الواحد، حسب متوسط
    شائع لموديلات زي Gemini/Groq. مفيدة للمستخدم يعرف حجم الطلب تقريبًا
    قبل ما يوافق على معالجة كمية كبيرة."""
    return max(1, char_count // 4)


def chunk_text(text: str, max_chars: int = MAX_CHARS_PER_CHUNK):
    """يقسم النص لمقاطع بحد أقصى max_chars، مع محاولة عدم قطع أي جملة نص
    نصين. لو جملة واحدة أطول من max_chars لوحدها (نادر)، بتتحط في مقطع
    مستقل بدل ما تتقطع بالغلط."""
    sentences = [s for s in _SENTENCE_END_RE.split(text) if s and s.strip()]
    chunks, current, length = [], [], 0
    for s in sentences:
        s_len = len(s) + 1
        if current and length + s_len > max_chars:
            chunks.append(" ".join(current))
            current, length = [], 0
        current.append(s)
        length += s_len
    if current:
        chunks.append(" ".join(current))
    return chunks or ([text] if text.strip() else [])


def summarize_new_part(lecture: str, full_text: str, state: dict, mode: str = "lecture") -> None:
    """
    يحوّل الجزء الجديد من النص لنوتس (اللي بعد آخر نقطة اتشرحت)،
    ويضيفه كقسم جديد في ملف الـ Markdown بتاريخ اليوم.

    mode: "lecture" (افتراضي) = أسلوب "لخص المحاضرة" التعليمي (EXPLAIN_PROMPT)،
          "meeting" = أسلوب "خد نوتس" المختصر لمحضر اجتماع (MEETING_NOTES_PROMPT).
    """
    prompt = MEETING_NOTES_PROMPT if mode == "meeting" else EXPLAIN_PROMPT

    new_text = full_text[state["summarized_chars"]:].strip()
    if not new_text:
        _log("[i] مفيش نص جديد يتشرح.")
        return

    # الملفات اللي هتتحسب "متشرّحة" بعد نجاح العملية دي = أي ملف اتفرغ
    # قبل كده بس لسه مش متعلّم عليه إنه اتشرح
    files_about_to_be_explained = [
        fn for fn in state["transcribed_files"]
        if fn not in state["explained_files"]
    ]

    chunks = chunk_text(new_text)
    total = len(chunks)
    _log(f"[i] بيحوّل الجزء الجديد لنوتس ({total} مقطع/مقاطع)...")

    partial_notes = []
    for i, c in enumerate(chunks, 1):
        if _cancelled():
            _log(f"[⏹] اتلغت العملية - اتشرح {i - 1}/{total} مقطع قبل الإلغاء.")
            if not partial_notes:
                return
            break

        _log(f"    [i] مقطع {i}/{total} ...")
        _progress(i - 1, total, "تحويل لنوتس")
        partial_notes.append(explain_text(c, prompt))
        _progress(i, total, "تحويل لنوتس")

    if len(partial_notes) == 1:
        final_notes = partial_notes[0]
    else:
        _log("[i] بيجمع نوتس كل المقاطع في نسخة نهائية متماسكة...")
        combined = "\n\n".join(partial_notes)
        final_notes = explain_text(combined, prompt)

    md_path = MARKDOWN_FOLDER / f"{lecture}.md"
    today = datetime.now().strftime("%Y-%m-%d %H:%M")

    header = f"# {lecture}\n\n" if not md_path.exists() else ""
    section = f"{header}## تحديث - {today}\n\n{final_notes}\n\n---\n\n"

    with open(md_path, "a", encoding="utf-8") as f:
        f.write(section)

    # نحتفظ بتاريخ (stack) للحالات قبل كل تحديث، مش قيمة واحدة بس - عشان
    # يمكن التراجع لأكتر من خطوة للخلف. كل عنصر خفيف جداً (رقم + أسماء
    # ملفات)، مفيش نسخ لمحتوى الملف نفسه لأن ده بيترجع من الهيدرز
    # ("## تحديث - ...") الموجودة في ملف الـ Markdown نفسه أصلاً (المصدر
    # الحقيقي الوحيد)، مش من نسخة احتياطية منفصلة ممكن تتعارض معاه.
    undo_stack = state.get("_undo_stack", [])
    undo_stack.append({
        "summarized_chars": state["summarized_chars"],
        "explained_files": list(state["explained_files"]),
    })
    state["_undo_stack"] = undo_stack[-MAX_UNDO_STEPS:]  # حد أقصى لعدد الخطوات
    state.pop("_undo", None)  # اسم قديم لباج قديم (single-level) - بنتخلص منه
    state["summarized_chars"] = len(full_text)
    state["explained_files"].extend(files_about_to_be_explained)
    with get_lecture_lock(lecture):
        save_state(lecture, state)

    _log(f"[✓] النوتس الجديدة اتضافت في: {md_path}")


# حد أقصى لعدد خطوات التراجع المحفوظة لكل محاضرة - عشان ملف الـ state
# مايكبرش من غير داعي لو حد عمل عشرات التحديثات على مدى شهور.
MAX_UNDO_STEPS = 10


def undo_last_notes_update(lecture: str) -> bool:
    """
    يلغي آخر تحديث نوتس: يشيل آخر قسم "## تحديث - ..." من ملف الـ
    Markdown (المصدر الحقيقي الوحيد لعدد الأقسام)، ويرجّع حالة التتبع
    (summarized_chars وexplained_files) لآخر نسخة محفوظة في الـ stack.
    قابلة للاستدعاء أكتر من مرة متتالية للتراجع لعدة خطوات للخلف (لحد
    MAX_UNDO_STEPS)، مش خطوة واحدة بس زي قبل كده.
    بيرجع True لو نجح، False لو مفيش تحديث يتلغى.
    """
    state = load_state(lecture)
    undo_stack = state.get("_undo_stack", [])
    md_path = MARKDOWN_FOLDER / f"{lecture}.md"

    if not undo_stack or not md_path.exists():
        return False

    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()

    idx = content.rfind("## تحديث - ")
    if idx == -1:
        return False

    new_content = content[:idx].rstrip("\n")
    header_only = new_content.strip() == f"# {lecture}".strip()

    if not new_content or header_only:
        md_path.unlink(missing_ok=True)
    else:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(new_content + "\n\n")

    backup = undo_stack.pop()
    state["summarized_chars"] = backup["summarized_chars"]
    state["explained_files"] = backup["explained_files"]
    state["_undo_stack"] = undo_stack
    with get_lecture_lock(lecture):
        save_state(lecture, state)

    return True


def main():
    if len(sys.argv) < 2:
        _log('الاستخدام: python process_lecture.py "اسم المحاضرة"')
        return

    lecture = sys.argv[1]
    state = load_state(lecture)

    full_text = transcribe_new_files(lecture, state)
    if not full_text:
        _log("[!] مفيش نص متفرغ للمحاضرة دي خالص.")
        return

    summarize_new_part(lecture, full_text, state)


if __name__ == "__main__":
    main()