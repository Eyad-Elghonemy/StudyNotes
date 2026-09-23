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

# لازم load_dotenv() تتنفذ قبل ما نعمل import لـ state_manager - لأن
# state_manager.py بيحسب مسار المجلدات (BASE_DIR) فورًا لحظة الـ import
# نفسه (كود على مستوى الملف)، ولو .env لسه ما اتقرأش وقتها، STUDYNOTES_DIR
# مش هيكون موجود في os.environ، فالمسار هيرجع غلط لمجلد السكريبت نفسه
# (system/) بدل المسار الصح من .env. الترتيب القديم (state_manager قبل
# load_dotenv) كان بيسبب إنشاء المجلدات (Markdown, Transcript,
# Sound_Recorded, .state) جوه مجلد system/ غلط لما الملف ده يتشغّل لوحده
# (مش عن طريق gui_app.py اللي ترتيبه كان صح أصلاً).
load_dotenv()

from state_manager import (
    TRANSCRIPT_FOLDER,
    MARKDOWN_FOLDER,
    load_state,
    save_state,
    delete_audio_file,
    get_lecture_lock,
    audio_duration_minutes_safe,
)

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
    (("quota", "rate limit", "429", "resource_exhausted", "tokens per minute", " tpm ", "requests per minute", " rpm "),
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

# كلمات مفتاحية بتدل إن الخطأ سببه تحديد معدل الطلبات (rate limit) عند
# المزوّد - سواء كود 429 القياسي أو 413 لما بيتحسب على أساس tokens-per-
# minute (زي Groq أحياناً). الأخطاء دي "مؤقتة" بطبيعتها وبتستاهل إعادة
# محاولة بعد استنى، عكس أخطاء زي مفتاح غلط اللي مفيش داعي نعيد نحاول فيها.
_RATE_LIMIT_KEYWORDS = (
    "429", "quota", "rate limit", "resource_exhausted",
    "tokens per minute", "tpm", "requests per minute", " rpm",
    "413", "too large", "too many requests",
)


def _is_rate_limit_error(e: Exception) -> bool:
    low = str(e).lower()
    return any(k in low for k in _RATE_LIMIT_KEYWORDS)


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

# NVIDIA NIM بيستخدم بس للتلخيص/النوتس (مش عنده Speech-to-Text)، وده fallback
# تالت اختياري - البرنامج شغال عادي تمامًا من غيره (زي أي مفتاح تاني).
# مجاني دائم من غير بطاقة ائتمان (build.nvidia.com)، وواجهته متوافقة مع
# OpenAI (نفس مكتبة openai القياسية، مش SDK خاص بيهم).
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
NVIDIA_TEXT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"

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
    "auto":            ("🔄 Auto (Gemini → Groq → NVIDIA)", None),
    "gemini_flash":    ("Gemini - gemini-3.6-flash (balanced)", ("gemini", "gemini-3.6-flash")),
    "gemini_lite":     ("Gemini - gemini-3.5-flash-lite (fastest)", ("gemini", "gemini-3.5-flash-lite")),
    "gemini_37":       ("Gemini - gemini-3.7-flash (strongest Gemini)", ("gemini", "gemini-3.7-flash")),
    "groq_gptoss120b": ("Groq - gpt-oss-120b (strongest)", ("groq", "openai/gpt-oss-120b")),
    "groq_gptoss20b":  ("Groq - gpt-oss-20b (fastest)", ("groq", "openai/gpt-oss-20b")),
    "groq_qwen36":     ("Groq - qwen3.6-27b", ("groq", "qwen/qwen3.6-27b")),
    "nvidia_ultra":    ("NVIDIA - nemotron-3-ultra-550b (strongest)", ("nvidia", "nvidia/nemotron-3-ultra-550b-a55b")),
    "nvidia_lightning": ("NVIDIA - nemotron-3.5-lightning-30b (fastest)", ("nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b")),
}

TRANSCRIBE_MODEL_CHOICE = os.environ.get("TRANSCRIBE_MODEL_CHOICE", "auto")
if TRANSCRIBE_MODEL_CHOICE not in TRANSCRIBE_MODEL_CHOICES:
    TRANSCRIBE_MODEL_CHOICE = "auto"

SUMMARY_MODEL_CHOICE = os.environ.get("SUMMARY_MODEL_CHOICE", "auto")
if SUMMARY_MODEL_CHOICE not in SUMMARY_MODEL_CHOICES:
    SUMMARY_MODEL_CHOICE = "auto"

# آخر مزوّد نجح فعليًا في كل مهمة (transcribe/summary) - بتتحدث لحظيًا كل
# ما محاولة تنجح (انظر transcribe_audio_file/explain_text تحت). الهدف
# الوحيد منها: لما الاختيار يكون "Auto"، الواجهة تقدر تعرض لوجو المزوّد
# اللي فعليًا بيشتغل دلوقتي بدل أيقونة عامة - مجرد قيمة في الذاكرة
# لحاجة عرض بصري بس، مش جزء من منطق الـ fallback نفسه ومش بتتحفظ بين
# التشغيلات (يرجعوا None تاني عند إعادة تشغيل البرنامج).
LAST_USED_TRANSCRIBE_PROVIDER = None
LAST_USED_SUMMARY_PROVIDER = None


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

# ------------------ إعدادات حماية من الـ Rate Limit ------------------
# استراحة قصيرة بين كل مقطع (chunk) والتاني وقت التلخيص، وبين كل ملف
# صوتي والتاني وقت التفريغ - عشان نوزّع الطلبات على الوقت (throttling)
# بدل ما نطلقها كلها متلاحقة ونضغط على سقف tokens-per-minute بتاع
# المزوّد فجأة.
CHUNK_PACING_SECONDS = 2.0

# لو حصل rate-limit error (429/413 tokens-per-minute) في نفس المزوّد،
# نستنى ونعيد المحاولة على نفس المزوّد بدل ما نسيب فورًا للمزوّد التاني
# (اللي غالبًا هيكون قريب من نفس المشكلة لو كان مستهلك برضو). الأوقات
# دي بتكبر تصاعديًا (exponential backoff) زي أي نظام بيتعامل مع API له حد.
RATE_LIMIT_RETRY_DELAYS = [15, 30]  # بالثواني

# أقصى عدد ملخصات جزئية بيتدمجوا في استدعاء واحد لمرحلة "التوحيد". لو
# عدد المقاطع أكبر من كده، بيتم الدمج على مراحل هرمية (batches) بدل
# استدعاء واحد ضخم - النص الطويل جدًا بيقلل تركيز الموديل على التفاصيل
# في نص/وسط النص (ظاهرة معروفة اسمها "Lost in the Middle")، وده بالظبط
# اللي كان بيسبب تدهور جودة التلخيص مع الجلسات الطويلة (10+ مقاطع).
MERGE_BATCH_SIZE = 4

# حد حجم الملف المسموح بيه على خطة Groq المجانية (25MB بدون تقطيع الصوت نفسه).
# لو الجزء أكبر من كده (مثلاً لو ffmpeg مش متثبت وفضل الملف FLAC خام)، نتخطى
# محاولة Groq تمامًا ونروح مباشرة لـ Gemini بدل ما نستنى فشل مؤكد.
GROQ_MAX_FILE_BYTES = 25 * 1024 * 1024

TRANSCRIBE_PROMPT = """فرّغ هذا التسجيل الصوتي إلى نص عربي كامل وحرفي (اكتب اللهجة
كما نُطقت، بدون ترجمتها للفصحى وبدون تلخيص). اكتب كل ما قيل بالترتيب،
بدون أي تعليق أو مقدمة منك، فقط النص المفرغ نفسه."""

# كل نصوص البرومبتات (EXPLAIN_PROMPT, SUBJECT_PROFILES, MEETING_NOTES_PROMPT,
# CONSOLIDATION_PROMPT, MEETING_CONSOLIDATION_PROMPT, build_explain_prompt)
# منقولة لملف prompts.py منفصل (separation of concerns - فصل منطق
# الاتصال بالـ API هنا عن محتوى التعليمات نفسها). الأسماء بتتستورد هنا
# بنفس الأسماء القديمة عشان أي كود تاني (gui_app.py مثلاً) يفضل شغال من
# غير أي تغيير.
from prompts import (
    EXPLAIN_PROMPT,
    MEETING_NOTES_PROMPT,
    CONSOLIDATION_PROMPT,
    MEETING_CONSOLIDATION_PROMPT,
    DEFAULT_SUBJECT_LABEL,
    SUBJECT_PROFILES,
    build_explain_prompt,
)


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
        fn = (lambda p=audio_path, m=model_id: _transcribe_with_groq(p, m)) if provider == "groq" \
            else (lambda p=audio_path: _transcribe_with_gemini(p))

        attempt = 0
        while True:
            _log(f"    → بيحاول عبر {label}...")
            status, result = _run_with_timeout(fn, timeout_sec)

            if status == "ok":
                _log(f"    ✓ خلص عبر {label} ({time.time() - t0:.1f} ثانية)")
                global LAST_USED_TRANSCRIBE_PROVIDER
                LAST_USED_TRANSCRIBE_PROVIDER = provider
                return result

            if status == "timeout":
                _log(f"    ⚠ {label} اتأخر عن المتوقع (~{timeout_sec:.0f} ثانية) - بيجرب التالي...")
                errors.append(f"{label}: اتأخر عن المتوقع")
                break

            if _is_rate_limit_error(result) and attempt < len(RATE_LIMIT_RETRY_DELAYS):
                delay = RATE_LIMIT_RETRY_DELAYS[attempt]
                attempt += 1
                _log(f"    ⏳ {label} وصل لحد الـ rate limit - بيستنى {delay} ثانية ويعيد نفس المزوّد ({attempt}/{len(RATE_LIMIT_RETRY_DELAYS)})...")
                time.sleep(delay)
                continue

            _log(f"    ⚠ {label} فشل ({friendly_error(result)}), بيجرب التالي...")
            errors.append(f"{label}: {friendly_error(result)}")
            break

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


def _explain_with_nvidia(text: str, prompt: str = EXPLAIN_PROMPT, model: str = None) -> str:
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY مش موجود")
    from openai import OpenAI

    # NVIDIA NIM بيستخدم واجهة متوافقة مع OpenAI بالكامل - نفس مكتبة
    # openai القياسية، بس بـ base_url مختلف.
    client = OpenAI(api_key=NVIDIA_API_KEY, base_url="https://integrate.api.nvidia.com/v1")
    completion = client.chat.completions.create(
        model=model or NVIDIA_TEXT_MODEL,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        # بعض موديلات NVIDIA (زي nemotron-3-ultra) عندها وضع "تفكير"
        # (chain-of-thought) بيرجع منفصل في reasoning_content. إحنا محتاجين
        # النص النهائي بس (النوتس)، فبنقفل الوضع ده عشان الرد يرجع مباشرة
        # في content من غير خطوات تفكير وسيطة نستهلك وقت/توكنز عليها من
        # غير فايدة.
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    result = (completion.choices[0].message.content or "").strip()
    if not result:
        raise RuntimeError("NVIDIA رجّع نوتس فاضية")
    return result


def _summary_candidates() -> list:
    """نفس فكرة _transcribe_candidates بس لقائمة اختيار موديل التلخيص."""
    default_chain = [("gemini", None), ("groq", None), ("nvidia", None)]
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
        if provider == "groq":
            label = f"Groq ({model_id})" if model_id else "Groq"
        elif provider == "gemini":
            label = f"Gemini ({model_id})" if model_id else "Gemini"
        elif provider == "nvidia":
            label = f"NVIDIA ({model_id})" if model_id else "NVIDIA"
        else:
            label = provider.capitalize()

        if provider == "groq":
            fn = lambda p=prompt, m=model_id: _explain_with_groq(text, p, m)
        elif provider == "nvidia":
            fn = lambda p=prompt, m=model_id: _explain_with_nvidia(text, p, m)
        else:
            fn = lambda p=prompt, m=model_id: _explain_with_gemini(text, p, m)

        # نجرب المزوّد ده لحد MAX (1 + عدد محاولات إعادة الاتصال) مرة،
        # لكن بس لو الفشل بسبب rate limit (مؤقت بطبيعته) - أي فشل تاني
        # (مفتاح غلط، صلاحيات...) بيتخطى فورًا للمزوّد التالي زي الأول.
        attempt = 0
        while True:
            _log(f"    → بيحاول عبر {label} (ممكن ياخد وقت حسب طول الجزء)...")
            status, result = _run_with_timeout(fn, timeout_sec)

            if status == "ok":
                _log(f"    ✓ خلص عبر {label} ({time.time() - t0:.1f} ثانية)")
                global LAST_USED_SUMMARY_PROVIDER
                LAST_USED_SUMMARY_PROVIDER = provider
                return result

            if status == "timeout":
                _log(f"    ⚠ {label} اتأخر عن المتوقع (~{timeout_sec:.0f} ثانية) - بيجرب التالي...")
                errors.append(f"{label}: اتأخر عن المتوقع")
                break

            if _is_rate_limit_error(result) and attempt < len(RATE_LIMIT_RETRY_DELAYS):
                delay = RATE_LIMIT_RETRY_DELAYS[attempt]
                attempt += 1
                _log(f"    ⏳ {label} وصل لحد الـ rate limit - بيستنى {delay} ثانية ويعيد نفس المزوّد ({attempt}/{len(RATE_LIMIT_RETRY_DELAYS)})...")
                time.sleep(delay)
                continue

            _log(f"    ⚠ {label} فشل ({friendly_error(result)}), بيجرب التالي...")
            errors.append(f"{label}: {friendly_error(result)}")
            break

    raise RuntimeError("فشل الشرح في كل المحاولات:\n  " + "\n  ".join(errors))


# كلمات بتدل إن الفشل سببه "الطلب نفسه أكبر من الحد" (مشكلة بنيوية في
# حجم النص المرسل)، مش استهلاك عام للحصة. الفرق مهم: لو استهلاك عام،
# الانتظار (retry) بيحل المشكلة. لو الطلب نفسه أكبر من الحد، الانتظار
# مش هيفرق - نفس النص هيرجع يفشل بنفس السبب - والحل الوحيد إننا نقسّم
# النص لأجزاء أصغر.
_STRUCTURAL_SIZE_KEYWORDS = ("too large", "request too large", "payload too large", "413")


def _is_structural_size_error(e) -> bool:
    return any(k in str(e).lower() for k in _STRUCTURAL_SIZE_KEYWORDS)


MAX_ADAPTIVE_SPLIT_DEPTH = 2  # أقصى عدد مرات تقسيم لنفس المقطع (2 = لحد ربع الحجم الأصلي)


def explain_text_with_split(text: str, prompt: str = EXPLAIN_PROMPT, _depth: int = 0) -> str:
    """
    غلاف حول explain_text() بيضيف تقسيم تكيّفي (adaptive splitting): لو
    النص فشل لأنه "كبير على حد الطلب الواحد" عند المزوّد (مش استهلاك عام
    بيتصلح بالانتظار)، بيقسّمه لنصين متساويين تقريبًا (عند أقرب نهاية
    جملة) ويجرب كل نص لوحده بشكل مستقل، لحد MAX_ADAPTIVE_SPLIT_DEPTH مرة
    تقسيم كحد أقصى - عشان نضمن إن أي نص، مهما كان طويل، له فرصة حقيقية
    ينجح بدل ما يفشل نهائي بسبب حده الأقصى عند مزوّد معيّن.
    """
    try:
        return explain_text(text, prompt)
    except Exception as e:
        too_short_to_split = len(text) < 500
        depth_exhausted = _depth >= MAX_ADAPTIVE_SPLIT_DEPTH
        if not _is_structural_size_error(e) or too_short_to_split or depth_exhausted:
            raise

        _log("    ✂ المقطع كبير على حد الطلب الواحد عند المزوّد - هنقسمه لنصين ونجرب كل نص لوحده...")
        mid = len(text) // 2
        split_at = text.rfind(". ", 0, mid)
        if split_at == -1:
            split_at = text.rfind(" ", 0, mid)
        if split_at == -1:
            split_at = mid
        first_half = text[:split_at + 1].strip()
        second_half = text[split_at + 1:].strip()

        note_1 = explain_text_with_split(first_half, prompt, _depth + 1)
        note_2 = explain_text_with_split(second_half, prompt, _depth + 1)
        return note_1 + "\n\n" + note_2


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
                        # عنوان فاصل واضح قبل كل جزء (زي اسم الملف الصوتي
                        # المصدر نفسه، اللي بقى فريندلي - "المحاضرة - Part
                        # N") عشان سهل تعرف كل جزء من النص جاي منين وانت
                        # بتراجع ملف الترانسكريبت التراكمي.
                        header = f"=== {audio_path.stem.replace('__', ' - ')} ===\n"
                        chunk = header + text + "\n\n"
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

                if i < total:
                    time.sleep(CHUNK_PACING_SECONDS)

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


def _merge_batch(notes_batch: list, consolidation_prompt: str) -> str:
    """
    يدمج مجموعة صغيرة (≤ MERGE_BATCH_SIZE) من الملخصات الجزئية في نص
    واحد متماسك، باستخدام برومبت التوحيد المناسب (مختلف جوهريًا عن
    برومبت التلخيص الأساسي - انظر CONSOLIDATION_PROMPT/
    MEETING_CONSOLIDATION_PROMPT في prompts.py). لو فشل الدمج (rate
    limit مثلاً)، بيرجع مجرد تجميع بسيط (concatenation) بدل ما يضيع
    المحتوى بالكامل.
    """
    if len(notes_batch) == 1:
        return notes_batch[0]
    combined = "\n\n".join(notes_batch)
    try:
        return explain_text_with_split(combined, consolidation_prompt)
    except Exception as e:
        _log(f"[!] فشل دمج مجموعة من {len(notes_batch)} أجزاء - هنسيبهم مجمّعين من غير توحيد صياغة.")
        _log(f"    السبب: {friendly_error(e) if isinstance(e, Exception) else e}")
        return combined


def _hierarchical_merge(notes_list: list, consolidation_prompt: str) -> str:
    """
    دمج هرمي (Hierarchical Merge): بدل ما نلزّق كل الملخصات الجزئية في
    استدعاء واحد ضخم لمرحلة "التوحيد" (بيضعف تركيز الموديل مع النصوص
    الطويلة)، بندمجهم على دفعات صغيرة (MERGE_BATCH_SIZE كحد أقصى لكل
    استدعاء)، وبعدين ندمج نتايج الدفعات دي مع بعض بنفس الطريقة، لحد ما
    يفضل نص نهائي واحد بس. مطابق تمامًا للملاحظة العملية إن الدمج بتاع
    3-4 أجزاء مع بعض بيدّي جودة أعلى من دمج 10+ مرة واحدة.
    """
    current = notes_list
    level = 1
    while len(current) > 1:
        batches = [current[i:i + MERGE_BATCH_SIZE] for i in range(0, len(current), MERGE_BATCH_SIZE)]
        if len(batches) > 1:
            _log(f"[i] مرحلة دمج (مستوى {level}): {len(current)} جزء → {len(batches)} مجموعة...")
        merged = []
        for batch in batches:
            merged.append(_merge_batch(batch, consolidation_prompt))
            if len(batch) > 1:
                time.sleep(CHUNK_PACING_SECONDS)
        current = merged
        level += 1
    return current[0] if current else ""


def summarize_new_part(
    lecture: str, full_text: str, state: dict, mode: str = "lecture",
    end_chars: int = None,
) -> bool:
    """
    يحوّل الجزء الجديد من النص لنوتس (اللي بعد آخر نقطة اتشرحت)،
    ويضيفه كقسم جديد في ملف الـ Markdown بتاريخ اليوم.

    end_chars: لو محدد، بيوقف عند الموضع ده بالظبط بدل ما ياخد لحد آخر
    النص كله - ده بيسمح بتلخيص جزء بس من النص الجديد (مثلاً لما المستخدم
    يحدد أجزاء معيّنة بس في "حوّل لنوتس بس")، بشرط إن الموضع ده يكون
    بعد نقطة البداية (base_summarized_chars) عشان يفضل الترتيب التسلسلي
    سليم (منقدرش "نقفز" ونسيب فجوة في النص من غير ما نلخصها).

    بيرجع True لو كل النص المطلوب (لحد end_chars أو لحد آخر full_text لو
    مش محدد) اتشرح بنجاح، وFalse لو وقف قبل ما يخلص.

    مبدأ الحماية من فقدان الشغل (checkpointing): لو مقطع فشل بعد كل
    محاولاته (مع كل مزوّدين ومحاولات إعادة الاتصال)، مش بنرمي استثناء
    يمسح المقاطع اللي نجحت قبله - بنحفظهم فعليًا في الـ md ونحدّث نقطة
    الوقوف (summarized_chars) لحد هناك بس، فلو ضغطت "لخّص" تاني، هيكمل
    من بعد آخر نقطة نجحت، مش من الأول.

    mode: "lecture" (افتراضي) = أسلوب "لخص المحاضرة" التعليمي (EXPLAIN_PROMPT)،
          "meeting" = أسلوب "خد نوتس" المختصر لمحضر اجتماع (MEETING_NOTES_PROMPT).
    """
    is_meeting = mode == "meeting"
    consolidation_prompt = MEETING_CONSOLIDATION_PROMPT if is_meeting else CONSOLIDATION_PROMPT

    def _chunk_prompt(context_carry: str) -> str:
        if is_meeting:
            if not context_carry:
                return MEETING_NOTES_PROMPT
            # نفس فكرة السياق المختصر بتاعة وضع المحاضرة - بيقلل تكرار
            # نفس المهمة/القرار في "القرارات والمهام" لو المتحدثين رجعوا
            # ذكروها في جزء تاني من نفس الاجتماع.
            return (
                f'سياق (آخر نقطة اتغطت في الجزء اللي قبل ده مباشرة، من نفس '
                f'الاجتماع): "{context_carry}"\n'
                "كمّل من هنا - من غير ما تكرر نفس المهمة أو القرار في قسم "
                '"القرارات والمهام" لو كانت اتوثقت بالفعل هناك.\n\n'
                + MEETING_NOTES_PROMPT
            )
        return build_explain_prompt(
            state.get("subject", ""),
            state.get("enable_corrections", False),
            state.get("enable_additions", False),
            context_carry=context_carry,
        )

    base_summarized_chars = state["summarized_chars"]
    text_end = len(full_text) if end_chars is None else max(base_summarized_chars, min(end_chars, len(full_text)))
    new_text = full_text[base_summarized_chars:text_end].strip()
    if not new_text:
        _log("[i] مفيش نص جديد يتشرح.")
        return True

    # الملفات اللي هتتحسب "متشرّحة" - بس هنعلّم عليها فعليًا في الآخر لو
    # *كل* المقاطع نجحت، وبس لو مدى النص بتاعها بالكامل واقع جوه النطاق
    # المطلوب شرحه (base_summarized_chars → text_end) - عشان لو المستخدم
    # حدد جزء بس من النص (end_chars)، منعلمش على ملفات جاية بعد النطاق
    # ده كإنها "خلصت شرحها" برضو.
    transcript_ranges = state.get("transcript_ranges", {})
    files_about_to_be_explained = [
        fn for fn in state["transcribed_files"]
        if fn not in state["explained_files"]
        and transcript_ranges.get(fn, [0, text_end])[1] <= text_end
    ]

    chunks = chunk_text(new_text)
    total = len(chunks)
    _log(f"[i] بيحوّل الجزء الجديد لنوتس ({total} مقطع/مقاطع)...")

    partial_notes = []
    processed_chars = 0
    stopped_early = False
    context_carry = ""
    for i, c in enumerate(chunks, 1):
        if _cancelled():
            _log(f"[⏹] اتلغت العملية - اتشرح {i - 1}/{total} مقطع قبل الإلغاء.")
            stopped_early = True
            break

        _log(f"    [i] مقطع {i}/{total} ...")
        _progress(i - 1, total, "تحويل لنوتس")
        try:
            note = explain_text_with_split(c, _chunk_prompt(context_carry))
        except Exception as e:
            _log(f"[!] فشل شرح المقطع {i}/{total} بعد كل المحاولات - هنوقف هنا ونحفظ اللي خلص لحد دلوقتي.")
            _log(f"    السبب: {friendly_error(e) if isinstance(e, Exception) else e}")
            stopped_early = True
            break

        partial_notes.append(note)
        # سياق مختصر (آخر ~220 حرف من النوتس اللي طلعت دلوقتي) للمقطع
        # الجاي - بيقلل تكرار نفس التعريف/الخلاصة عبر حدود المقاطع من
        # غير ما يزوّد حجم البرومبت بشكل ملحوظ.
        context_carry = note[-220:].strip() if note else ""

        processed_chars += len(c) + 1  # +1 تقريبي بسبب الفاصل اللي بيضيفه chunk_text بين الجمل
        _progress(i, total, "تحويل لنوتس")

        if i < total:
            time.sleep(CHUNK_PACING_SECONDS)

    if not partial_notes:
        return False  # ولا مقطع واحد نجح - مفيش حاجة نحفظها

    if len(partial_notes) == 1:
        final_notes = partial_notes[0]
    else:
        _log("[i] بيجمع نوتس كل المقاطع في نسخة نهائية متماسكة (دمج هرمي على دفعات)...")
        final_notes = _hierarchical_merge(partial_notes, consolidation_prompt)

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

    completed_all = (len(partial_notes) == total) and not stopped_early
    if completed_all:
        # كل المقاطع نجحت - الـ checkpoint بيبقى نهاية النطاق المطلوب
        # (text_end، مش بالضرورة نهاية full_text لو كان فيه end_chars
        # محدد)، وكل الملفات المرتبطة (جوه النطاق ده بس) بيتم تعليمها
        # كـ"متشرّحة".
        state["summarized_chars"] = text_end
        state["explained_files"].extend(files_about_to_be_explained)
    else:
        # نجاح جزئي بس - الـ checkpoint بيبقى لحد آخر مقطع نجح فعليًا
        # (تقريبي، مبني على مجموع أطوال المقاطع الناجحة)، ومنعلمش أي
        # ملف كـ"متشرّح" لحد ما كل النص بتاعه يخلص فعليًا - أأمن، وبيمنع
        # حذفه بالغلط من أي عملية cleanup مستقبلية.
        state["summarized_chars"] = base_summarized_chars + processed_chars

    with get_lecture_lock(lecture):
        save_state(lecture, state)

    if completed_all:
        _log(f"[✓] النوتس الجديدة اتضافت في: {md_path}")
    else:
        remaining = total - len(partial_notes)
        _log(f"[✓] اتحفظ اللي خلص لحد دلوقتي في: {md_path}")
        _log(f"[i] فاضل {remaining} مقطع/مقاطع - دوس زرار 'لخّص' تاني وقت ما تحب عشان تكمل، هيكمل من هنا مش من الأول.")

    return completed_all


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