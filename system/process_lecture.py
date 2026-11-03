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

# لازم السطر ده يتنفذ *قبل* "from google import genai" تحت - عشان كده
# حطيناه هنا قبل كل حاجة بدل ما نستنى state_manager يتستورد (تفاصيل
# المشكلة والحل في state_manager.py نفسه، بحث عن "tornado").
sys.modules.setdefault("tornado", None)

from dotenv import load_dotenv
from google import genai

# نستورد state_manager الأول عشان يحسب BASE_DIR (مكان بيانات اليوزر) -
# مبقاش محتاج .env خالص عشان يحسبه (راجع الشرح في state_manager.py) -
# وبعدين نحمّل .env من جوه المكان ده بالظبط.
from state_manager import (
    BASE_DIR,
    TRANSCRIPT_FOLDER,
    MARKDOWN_FOLDER,
    load_state,
    save_state,
    delete_audio_file,
    get_lecture_lock,
    audio_duration_minutes_safe,
)

load_dotenv(BASE_DIR / ".env")

import remote_config


def _tuning() -> dict:
    """قراءة أرقام الضبط (circuit breaker, حد تقسيم Groq الاستباقي...)
    من remote_config - بتترجع للقيم المدمجة في الكود تلقائيًا لو مفيش
    نت/كاش (راجع HARDCODED_DEFAULT جوه remote_config.py)."""
    return remote_config.get_tuning()


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


class _CancelledError(Exception):
    """بترمى من جوه أي نقطة انتظار/تكرار داخلية (مش بس اللوبات الخارجية
    القديمة اللي بتفحص _cancelled() بين كل ملف/مقطع) لحظة ما اليوزر يدوس
    "إلغاء" - زي نص انتظار rate-limit (15-30 ثانية) أو وسط لوب تجربة
    موديلات مختلفة لنفس المقطع. لازم تتعامل معاها كحالة خاصة (توقف نضيف،
    مش "فشل عادي") في أي except Exception عام ممكن تمر عليه وهي طالعة -
    وإلا هتتبلع بالغلط وكإن العملية فشلت عادي بدل ما تتلغي فعليًا."""


def _cancellable_sleep(seconds: float) -> None:
    """زي time.sleep(seconds) بالظبط، إلا إنها بتفحص _cancelled() كل
    0.2 ثانية بدل ما تستني المدة كاملة من غير فايدة - فلو اليوزر ضغط
    "إلغاء" وسط انتظار rate-limit (ممكن يوصل 30 ثانية)، العملية بتتوقف
    خلال أجزاء من الثانية بدل ما تستني الانتظار يخلص عادي الأول."""
    step = 0.2
    elapsed = 0.0
    while elapsed < seconds:
        if _cancelled():
            raise _CancelledError()
        time.sleep(min(step, seconds - elapsed))
        elapsed += step
    if _cancelled():
        raise _CancelledError()


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


# سقف "Output Tokens Per Minute" (OTPM) عند بعض الموديلات (زي qwen3.8-27b
# على Groq) - ده مختلف جوهريًا عن rate limit عادي: الخطأ نفسه بيقول صراحة
# "الحجم المتوقع للـ output تعدّى السقف المفروض" (exceed the enforced
# limit) - يعني المهمة نفسها (حجم النص/النوتس المطلوبة) أكبر من سقف
# الموديل البنيوي، مش ازدحام مؤقت. إعادة المحاولة لنفس الطلب (حتى بعد
# استنى) هتفشل بنفس السبب بالظبط - الحل الوحيد الفعلي هو موديل بسقف
# أعلى، مش الانتظار. لازم نفرّقه عن rate limit العادي عشان منضيعش وقت
# (دقيقة كاملة أحيانًا) في إعادة محاولات محكوم عليها بالفشل.
_STRUCTURAL_OTPM_KEYWORDS = (
    "output tokens per minute", "otpm", "expected output tokens exceed",
)


def _is_structural_otpm_error(e: Exception) -> bool:
    low = str(e).lower()
    return any(k in low for k in _STRUCTURAL_OTPM_KEYWORDS)


# =========================================================================
# Circuit Breaker عام: بيتتبع "صحة" كل (provider, model_id) لوحده، مش كل
# مزوّد ككل - جرّبنا عمليًا إن موديلات مختلفة لنفس المزوّد (زي Gemini
# 3.6 و3.8) بتفشل/تنجح بشكل مستقل عن بعض من لحظة للتانية.
#
# الحالات التلاتة (نمط Circuit Breaker المعياري):
#   🟢 Closed (طبيعي): بيتجرب عادي.
#   🔴 Open (متجنّب): بعد CIRCUIT_FAILURE_THRESHOLD فشلة متتالية، يتخطّى
#      فورًا (من غير أي محاولة اتصال) لمدة التهدئة الحالية بتاعته.
#   🟡 Half-Open (اختبار): بعد ما مدة التهدئة تخلص، أول محاولة عادية
#      جاية (مش طلب إضافي زيادة) بتبقى هي الاختبار - نجحت؟ يرجع Closed
#      فورًا (ومدة التهدئة ترجع للحد الأدنى). فشلت؟ يرجع Open، ومدة
#      التهدئة الجاية *تتضاعف* (Exponential Backoff، بحد أقصى) - عشان
#      عطل طويل (زي ساعة كاملة) ميسبّبش عشرات محاولات الاختبار المتكررة،
#      من غير ما نضحّي بسرعة اكتشاف "رجع شغال" في الأعطال القصيرة.
#
# بيتصفّر بالكامل لما البرنامج يتقفل ويتفتح تاني (مش بيتحفظ على القرص) -
# كافي لمشكلة زي "ساعة فيها Gemini واقع"، ومفيش داعي تعقيد أكتر من كده.
# =========================================================================
# القيم دي بقت تتقرى من remote_config._tuning() لحظيًا بدل ثوابت
# مجمّدة - شوف _tuning() فوق واستخداماتها تحت في _circuit_record_failure.

_circuit_state: dict = {}  # (provider, model_id) -> {"failures", "opened_at", "cooldown"}


def _circuit_key(provider: str, model_id: str | None) -> tuple:
    return (provider, model_id or "__default__")


def _circuit_available(provider: str, model_id: str | None) -> bool:
    """متاح (Closed) أو في فترة الاختبار (Half-Open)؟ الاتنين بيترجموا
    لـ True هنا - الفرق الوحيد عملياً إن Half-Open بيبقى "اختبار" لو
    فشل، لكن من ناحية "نجرب ولا لأ" نفس القرار."""
    state = _circuit_state.get(_circuit_key(provider, model_id))
    if state is None or state["opened_at"] is None:
        return True  # لسه ماتجنبش خالص
    return (time.time() - state["opened_at"]) >= state["cooldown"]  # Half-Open لو التهدئة خلصت


def _circuit_record_success(provider: str, model_id: str | None) -> None:
    # نجاح (حتى لو كان "اختبار" بعد فترة تهدئة) بيصفّر كل حاجة، بما فيها
    # مدة التهدئة نفسها - ترجع للحد الأدنى من تاني فشل جديد يحصل بعدها.
    _circuit_state[_circuit_key(provider, model_id)] = {
        "failures": 0, "opened_at": None, "cooldown": _tuning()["circuit_base_cooldown"],
    }


def _circuit_reset_provider(provider: str, except_model: str | None = None) -> None:
    """يصفّر دوائر كل موديلات provider معيّن (ماعدا except_model اللي
    اتصفّر أصلاً من _circuit_record_success) - بيتنادى لما موديل تاني
    من نفس المزوّد ينجح في سياق اختيار يدوي لموديل واقع، كدليل إن
    المزوّد نفسه رجع يشتغل - عشان الموديل المختار يدوي ميفضلش متجنّب
    لحد ما مدة التهدئة تخلص لوحدها. مقصورة على التلخيص اليدوي بس (قرار
    متعمد، مش عام على كل حالات Auto)."""
    for key in list(_circuit_state.keys()):
        if key[0] == provider and key[1] != except_model:
            _circuit_state.pop(key, None)


def _circuit_record_failure(provider: str, model_id: str | None) -> None:
    tuning = _tuning()
    key = _circuit_key(provider, model_id)
    state = _circuit_state.get(key, {"failures": 0, "opened_at": None, "cooldown": tuning["circuit_base_cooldown"]})
    state["failures"] += 1
    if state["failures"] >= tuning["circuit_failure_threshold"]:
        if state["opened_at"] is not None:
            # كانت مفتوحة بالفعل، وده فشل محاولة الاختبار (Half-Open) -
            # يبقى العطل لسه مستمر: ضاعف مدة التهدئة الجاية (بحد أقصى).
            state["cooldown"] = min(state["cooldown"] * 2, tuning["circuit_max_cooldown"])
        state["opened_at"] = time.time()  # افتح الدائرة (أو جدّدها بالمدة الجديدة)
    _circuit_state[key] = state


def friendly_error(e: Exception) -> str:
    """يرجع نسخة مبسّطة من رسالة الخطأ لو عرف يتعرف عليها، وإلا بيرجع
    النص الخام زي ما هو (احتياطي، أفضل من ما نخفي معلومة قد تفيد)."""
    raw = str(e)
    low = raw.lower()
    for keywords, friendly in _ERROR_PATTERNS:
        if any(k in low for k in keywords):
            return f"{friendly}  (التفاصيل الخام: {raw[:500]})"
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
# ده بقى ديناميكي دلوقتي: بيتبني لحظيًا من سلسلة remote_config (بعد فلترة
# أي provider مفيش مفتاحه) بدل array ثابت هنا - فأي موديل يتضاف/يتشال/
# يتعطّل من model_config.json على GitHub بيظهر أو يختفي من الـ dropdown
# في الواجهة تلقائيًا، من غير أي تعديل أو إصدار نسخة جديدة من البرنامج.
#
# مفتاح كل اختيار بقى "{provider}:{model أو 'default'}" (ثابت ومتوقع، مش
# اسم مختصر يدوي زي الأول) عشان القيمة المحفوظة في .env تفضل صالحة طول
# ما الموديل لسه موجود في السلسلة، أيًا كان ترتيبه أو مكانه.

_PROVIDER_LABEL = {"gemini": "Gemini", "groq": "Groq", "nvidia": "NVIDIA"}

# أسامي عرض مخصصة لموديلات معروفة (أوضح من الاسم التقني الخام) - مش
# إلزامية: أي موديل مش موجود هنا بيتعرض باسمه الخام تلقائيًا (fallback
# في _display_name_for تحت)، فإضافة موديل جديد من بعيد مش محتاجة تعديل
# هنا خالص عشان يشتغل، بس ممكن تضيفه هنا لاحقًا لو عايز اسم أوضح.
_MODEL_DISPLAY_OVERRIDES = {
    ("gemini", "gemini-3.8-flash"): "Gemini - Newest, experimental (gemini-3.8-flash)",
    ("gemini", "gemini-3.7-flash"): "Gemini - Strongest (gemini-3.7-flash)",
    ("gemini", "gemini-3.6-flash"): "Gemini - Balanced (gemini-3.6-flash)",
    ("gemini", "gemini-3.5-flash-lite"): "Gemini - Fastest (gemini-3.5-flash-lite)",
    ("groq", "openai/gpt-oss-120b"): "Groq - Strongest (gpt-oss-120b)",
    ("groq", "openai/gpt-oss-20b"): "Groq - Fastest (gpt-oss-20b)",
    ("groq", "qwen/qwen3.8-27b"): "Groq - Balanced (qwen3.8-27b)",
    ("nvidia", "nvidia/nemotron-3-ultra-550b-a55b"): "NVIDIA - Strongest, slow (nemotron-3-ultra-550b)",
    ("nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b"): "NVIDIA - Fastest (nemotron-3.5-lightning-30b)",
    ("groq", "whisper-large-v3"): "Groq - Accurate (whisper-large-v3)",
    ("groq", "whisper-large-v3-turbo"): "Groq - Fast (whisper-large-v3-turbo)",
    ("gemini", None): "Gemini",
}

# مفاتيح قديمة كانت بتتحفظ في .env قبل ما الكتالوج يبقى ديناميكي - لسه
# لازمة عشان مستخدم فاتح نسخة قديمة اختار موديل يدوي، النسخة الجديدة متعرفش
# تسيبه "عالق" على اختيار مش موجود، وترجّعه لنفس الموديل (لو لسه في
# السلسلة) بدل ما ترجعه auto بالغلط.
_LEGACY_SUMMARY_KEYS = {
    "gemini_37": ("gemini", "gemini-3.7-flash"),
    "gemini_flash": ("gemini", "gemini-3.6-flash"),
    "gemini_38": ("gemini", "gemini-3.8-flash"),
    "gemini_lite": ("gemini", "gemini-3.5-flash-lite"),
    "groq_gptoss120b": ("groq", "openai/gpt-oss-120b"),
    "groq_gptoss20b": ("groq", "openai/gpt-oss-20b"),
    "groq_qwen38": ("groq", "qwen/qwen3.8-27b"),
    "nvidia_ultra": ("nvidia", "nvidia/nemotron-3-ultra-550b-a55b"),
    "nvidia_lightning": ("nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b"),
}
_LEGACY_TRANSCRIBE_KEYS = {
    "groq_v3": ("groq", "whisper-large-v3"),
    "groq_turbo": ("groq", "whisper-large-v3-turbo"),
    "gemini": ("gemini", None),
}


def _display_name_for(provider: str, model: str | None) -> str:
    override = _MODEL_DISPLAY_OVERRIDES.get((provider, model))
    if override:
        return override
    label = _PROVIDER_LABEL.get(provider, provider.title())
    if model:
        return f"{label} ({model.split('/')[-1]})"
    return label


def _model_choice_key(provider: str, model: str | None) -> str:
    return f"{provider}:{model or 'default'}"


def _short_name_for(provider: str, model: str | None) -> str:
    """نسخة مختصرة من الاسم (بدون القوسين/الوصف) - تستخدم في نص الفلو
    عشان يفضل قصير وسهل القراءة بدل تكرار الوصف الكامل كل مرة."""
    full = _display_name_for(provider, model)
    return full.split(" (")[0].split(" - ")[0]


def _build_model_choices(chain: list, auto_label: str) -> dict:
    choices = {"auto": (auto_label, None)}
    for provider, model in chain:
        choices[_model_choice_key(provider, model)] = (_display_name_for(provider, model), (provider, model))
    return choices


def _auto_flow_short_label(chain: list, max_items: int = 3) -> str:
    """نص ثابت لعنصر "auto" في الـ dropdown - من غير معاينة للسلسلة
    (كانت بتبان مزحومة/طويلة جوه الـ dropdown نفسه). تفاصيل السلسلة
    الكاملة موجودة تحت في قسم "Current Auto fallback order" بدل ما
    تتكرر هنا كمان."""
    return "🔄 Auto (recommended)"


# أسامي مختصرة جدًا (بس الهوية + الرقم/الدرجة) لعرضها في chips الفلو -
# النسخة الكاملة (_display_name_for) فيها تفاصيل تقنية (اسم الموديل
# الخام بين قوسين) مفيدة في الـ dropdown لما بتختار يدوي، بس زيادة عن
# اللزوم في سطر الفلو اللي بيعرض 7-9 موديلات مع بعض.
_MODEL_SHORT_LABEL = {
    ("gemini", "gemini-3.8-flash"): "Gemini 3.8",
    ("gemini", "gemini-3.7-flash"): "Gemini 3.7",
    ("gemini", "gemini-3.6-flash"): "Gemini 3.6",
    ("gemini", "gemini-3.5-flash-lite"): "Gemini 3.5 Lite",
    ("groq", "openai/gpt-oss-120b"): "Groq 120b",
    ("groq", "openai/gpt-oss-20b"): "Groq 20b",
    ("groq", "qwen/qwen3.8-27b"): "Groq Qwen",
    ("nvidia", "nvidia/nemotron-3-ultra-550b-a55b"): "NVIDIA Ultra",
    ("nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b"): "NVIDIA Lightning",
    ("groq", "whisper-large-v3"): "Groq Accurate",
    ("groq", "whisper-large-v3-turbo"): "Groq Fast",
    ("gemini", None): "Gemini",
}


def _short_chip_label(provider: str, model: str | None) -> str:
    override = _MODEL_SHORT_LABEL.get((provider, model))
    if override:
        return override
    label = _PROVIDER_LABEL.get(provider, provider.title())
    if model:
        # آخر جزء من اسم الموديل بس، مقصوص لو طويل جدًا - عشان موديل
        # جديد يتضاف من model_config.json بدون override يفضل مقروء.
        short = model.split("/")[-1]
        return f"{label} {short[:14]}"
    return label


def get_summary_model_choices() -> dict:
    """يبني كتالوج اختيار موديل التلخيص لحظيًا من remote_config. ده اللي
    الواجهة لازم تستخدمه بدل SUMMARY_MODEL_CHOICES (اللي بقت دالة، مش
    قاموس ثابت، من غير ما نلمس باقي الكود اللي بيعتمد على شكلها)."""
    chain = _filter_available_providers(remote_config.get_summary_chain())
    return _build_model_choices(chain, _auto_flow_short_label(chain))


def get_transcribe_model_choices() -> dict:
    chain = _filter_available_providers(remote_config.get_transcribe_chain())
    return _build_model_choices(chain, _auto_flow_short_label(chain))


def get_model_flow_items(kind: str) -> list:
    """ليستة [(provider, short_label), ...] لعرضها كـ chips ملوّنة في
    نافذة الإعدادات - أسامي مختصرة (Gemini 3.7 مش "Gemini - Strongest
    (gemini-3.7-flash)") عشان سطر الفلو يفضل قصير وسهل القراءة حتى مع
    7-9 موديلات مع بعض. نفس مصدر الحقيقة (candidates الفعلية) زي
    describe_model_flow، فمفيش احتمال الاتنين يختلفوا."""
    chain = _summary_candidates() if kind == "summary" else _transcribe_candidates()
    return [(p, _short_chip_label(p, m)) for p, m in chain]


def describe_model_flow(kind: str) -> str:
    """نص كامل (مش مختصر) لعرضه في نافذة Model Settings - بيوصف ترتيب
    محاولة الموديلات الفعلي دلوقتي (الاختيار اليدوي لو موجود، وإلا Auto
    الخام). مبني لحظيًا من _summary_candidates/_transcribe_candidates -
    نفس الدالة اللي بتتحكم في السلوك الفعلي وقت التلخيص/التفريغ، مش نسخة
    منفصلة ممكن تختلف عنها - فلو السلسلة اتغيّرت (موديل اتضاف/اتشال/اترتب
    من model_config.json، أو المستخدم غيّر اختياره اليدوي) النص هنا
    بيتحدّث تلقائي من غير أي تعديل كود.
    kind: "summary" أو "transcribe".
    """
    chain = _summary_candidates() if kind == "summary" else _transcribe_candidates()
    if not chain:
        return "⚠ مفيش موديلات متاحة دلوقتي - تأكد من مفاتيح الـ API."
    return " → ".join(_display_name_for(p, m) for p, m in chain)


def _normalize_choice_key(choice_raw: str, choices_dict: dict, legacy_map: dict) -> str:
    """بترجع المفتاح الصح لعرضه في الواجهة (قيمة موجودة فعلاً في
    choices_dict الديناميكي): نفس القيمة لو موجودة، أو ترجمة مفتاح قديم
    (legacy) لمفتاحه الديناميكي المقابل لو لسه الموديل ده موجود في
    السلسلة، أو "auto" في أي حالة تانية (موديل اتشال خالص من
    model_config.json مثلاً). من غير الدالة دي، فتح نافذة الإعدادات
    بمفتاح قديم محفوظ في .env كان بيعمل KeyError."""
    if choice_raw in choices_dict:
        return choice_raw
    if choice_raw in legacy_map:
        provider, model = legacy_map[choice_raw]
        dynamic_key = _model_choice_key(provider, model)
        if dynamic_key in choices_dict:
            return dynamic_key
    return "auto"


def _resolve_manual_choice(choice_raw: str, chain: list, legacy_map: dict):
    """بيحوّل قيمة الاختيار المحفوظة (مفتاح ديناميكي حالي، أو مفتاح قديم
    من نسخة أقدم) لـ (provider, model) - أو None لو الاختيار مش معروف
    خالص (موديل اتشال من model_config.json مثلاً)، وقتها بيتعامل كـ auto."""
    key_to_tuple = {_model_choice_key(p, m): (p, m) for p, m in chain}
    if choice_raw in key_to_tuple:
        return key_to_tuple[choice_raw]
    if choice_raw in legacy_map:
        return legacy_map[choice_raw]
    return None


TRANSCRIBE_MODEL_CHOICE = os.environ.get("TRANSCRIBE_MODEL_CHOICE", "auto")
SUMMARY_MODEL_CHOICE = os.environ.get("SUMMARY_MODEL_CHOICE", "auto")
# ملاحظة: متعملش validation هنا ضد الكتالوج الديناميكي - ده هيحتاج يستدعي
# remote_config (وممكن يعمل طلب شبكة) وقت استيراد الموديول نفسه، يعني
# وقت فتح البرنامج، وده بيبطّئ الفتح. الـ validation/الرجوع لـ auto بيحصل
# لحظيًا في _summary_candidates/_transcribe_candidates (عبر
# _resolve_manual_choice) من غير ما يأثر على سرعة فتح البرنامج.

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
    env_path = BASE_DIR / ".env"
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
    if choice_key != "auto" and choice_key not in get_transcribe_model_choices():
        return
    TRANSCRIBE_MODEL_CHOICE = choice_key
    os.environ["TRANSCRIBE_MODEL_CHOICE"] = choice_key
    _save_choice_to_env("TRANSCRIBE_MODEL_CHOICE", choice_key)


def set_summary_model_choice(choice_key: str) -> None:
    """نفس فكرة set_transcribe_model_choice، لاختيار موديل التلخيص/النوتس."""
    global SUMMARY_MODEL_CHOICE
    if choice_key != "auto" and choice_key not in get_summary_model_choices():
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

# كل نصوص البرومبتات (EXPLAIN_PROMPT, SUBJECT_PROFILES, build_explain_prompt,
# CONSOLIDATION_PROMPT, build_meeting_prompt, build_meeting_consolidation_prompt)
# منقولة لملف prompts.py منفصل (separation of concerns - فصل منطق
# الاتصال بالـ API هنا عن محتوى التعليمات نفسها). الأسماء بتتستورد هنا
# بنفس الأسماء القديمة عشان أي كود تاني (gui_app.py مثلاً) يفضل شغال من
# غير أي تغيير.
from prompts import (
    EXPLAIN_PROMPT,
    CONSOLIDATION_PROMPT,
    DEFAULT_SUBJECT_LABEL,
    SUBJECT_PROFILES,
    MEETING_SUBJECT_PROFILES,
    build_explain_prompt,
    build_meeting_prompt,
    build_meeting_consolidation_prompt,
)


# =========================================================================
# طبقة الاتصال بالنماذج
# =========================================================================

def _gemini_client() -> "genai.Client":
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY مش موجود")
    # بدون http_options هنا، مكتبة google-genai بتعمل إعادة محاولة داخلية
    # (عن طريق tenacity) لوحدها لما يحصل 503 - اكتشفنا إن ده ممكن يضيّع
    # 100+ ثانية في طلب واحد بس قبل ما يوصلنا الخطأ أصلاً. retry_options
    # بـ attempts=1 بيلغي إعادة المحاولة دي، فلو الموديل واقع، نعرف
    # بسرعة (ثواني) بدل ما نستنى المكتبة تستسلم من جوّاها.
    # ⚠️ لسه محتاج تأكيد عملي إن اسم الإعداد ده صح 100% مع نسخة المكتبة
    # عندك - جرّبه بـ test_summary_models.py وشوف لو الوقت قل فعلاً.
    try:
        http_options = genai.types.HttpOptions(retry_options={"attempts": 1})
        return genai.Client(api_key=GEMINI_API_KEY, http_options=http_options)
    except Exception:
        # لو الإعداد ده مش مدعوم في نسخة المكتبة الحالية، نكمل عادي من
        # غيره بدل ما نوقف البرنامج كله بسبب تحسين سرعة مش أساسي.
        return genai.Client(api_key=GEMINI_API_KEY)


def _transcribe_with_gemini(audio_path, model: str = None) -> str:
    client = _gemini_client()
    uploaded = client.files.upload(file=str(audio_path))
    try:
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
    finally:
        # نمسح الملف المؤقت من سيرفرات Gemini بعد الاستخدام - سواء التفريغ
        # نجح أو فشل. من غير المسح ده، الملفات بتفضل متراكمة لحد ما توصل
        # سقف الحساب (20GB)، ووقتها كل عمليات التفريغ الجاية بتترفض -
        # Bug #10 في تقرير المراجعة. فشل المسح نفسه (لو حصل، نادر) مش
        # لازم يأثر على نتيجة التفريغ - تحذير في اللوج بس، مش استثناء.
        try:
            client.files.delete(name=uploaded.name)
        except Exception as e:
            _log(
                f"    ⚠ تعذر مسح الملف المؤقت من سيرفرات Gemini "
                f"({uploaded.name}): {e} - هيتصفّى تلقائي بعد فترة لوحده."
            )


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
    default_chain = _filter_available_providers(remote_config.get_transcribe_chain())

    if TRANSCRIBE_MODEL_CHOICE == "auto":
        return default_chain

    chosen = _resolve_manual_choice(TRANSCRIBE_MODEL_CHOICE, default_chain, _LEGACY_TRANSCRIBE_KEYS)
    if chosen is None:
        return default_chain
    if chosen not in default_chain:
        default_chain = [chosen] + default_chain  # احتياطي: اليدوي معطّل/مفيش مفتاحه في remote config

    # الموديل المختار يدوي بييجي الأول، وبعده باقي السلسلة بترتيبها
    # الحرفي زي ما هي في model_config.json - من غير أي تجميع لموديلات
    # نفس المزوّد مع بعض (قرار متعمد: الترتيب في الملف هو مصدر الحقيقة
    # الوحيد، حتى لو كان معناه موديلات نفس المزوّد مش متجاورة).
    rest = [c for c in default_chain if c != chosen]
    return [chosen] + rest


def transcribe_audio_file(audio_path) -> str:
    """
    بيجرب المرشحين بالترتيب اللي رجعه _transcribe_candidates() (المختار
    يدويًا الأول لو موجود، وبعده الافتراضي كـ fallback). لكل محاولة، لو
    اتأخرت عن وقت متوقع (تقريبي، مبني على مدة الصوت) بيتجاوزها للتالي
    فورًا من غير ما يستناها - مش بس لو فشلت بـ exception.
    """
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
    available = [c for c in candidates if _circuit_available(c[0], c[1])]
    if available:
        candidates = available
    else:
        _circuit_state.clear()

    errors = []

    for provider, model_id in candidates:
        if _cancelled():
            raise _CancelledError()

        if provider == "groq" and size_bytes > GROQ_MAX_FILE_BYTES:
            errors.append("Groq: الملف أكبر من حد الـ 25MB المسموح بيه")
            continue

        label = f"Groq ({model_id})" if provider == "groq" else "Gemini"
        fn = (lambda p=audio_path, m=model_id: _transcribe_with_groq(p, m)) if provider == "groq" \
            else (lambda p=audio_path: _transcribe_with_gemini(p))

        attempt = 0
        while True:
            t0 = time.time()  # من جديد قبل كل محاولة - نفس إصلاح explain_text
            _log(f"    → بيحاول عبر {label}...")
            status, result = _run_with_timeout(fn, timeout_sec)

            if status == "ok":
                _log(f"    ✓ خلص عبر {label} ({time.time() - t0:.1f} ثانية)")
                _circuit_record_success(provider, model_id)
                global LAST_USED_TRANSCRIBE_PROVIDER
                LAST_USED_TRANSCRIBE_PROVIDER = provider
                return result

            if status == "timeout":
                _log(f"    ⚠ {label} اتأخر عن المتوقع (~{timeout_sec:.0f} ثانية) - بيجرب التالي...")
                errors.append(f"{label}: اتأخر عن المتوقع")
                _circuit_record_failure(provider, model_id)
                break

            if _is_structural_otpm_error(result):
                # سقف Output Tokens Per Minute بنيوي - الطلب نفسه أكبر من
                # الحد المسموح، مش ازدحام مؤقت. إعادة المحاولة هتفشل بنفس
                # السبب بالظبط، فنتخطى فورًا للموديل التالي من غير استنى.
                _log(f"    ⚠ {label} سقفه (OTPM) أصغر من حجم المهمة دي بنيويًا - بيتخطاه فورًا (مش rate limit عادي)...")
                errors.append(f"{label}: سقف OTPM بنيوي أصغر من حجم الطلب")
                _circuit_record_failure(provider, model_id)
                break

            if _is_rate_limit_error(result) and attempt < len(RATE_LIMIT_RETRY_DELAYS):
                delay = RATE_LIMIT_RETRY_DELAYS[attempt]
                attempt += 1
                _log(f"    ⏳ {label} وصل لحد الـ rate limit - بيستنى {delay} ثانية ويعيد نفس المزوّد ({attempt}/{len(RATE_LIMIT_RETRY_DELAYS)})...")
                _cancellable_sleep(delay)
                continue

            _log(f"    ⚠ {label} فشل ({friendly_error(result)}), بيجرب التالي...")
            errors.append(f"{label}: {friendly_error(result)}")
            _circuit_record_failure(provider, model_id)
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


# حد أقصى لعدد توكينز الـ Output لموديلات Groq معيّنة ليها سقف Output
# Tokens Per Minute (OTPM) صارم في خطتنا (زي qwen/qwen3.8-27b: 1000
# توكن/دقيقة بس) - طلب تلخيص عادي (نوتس مفصّلة من مقطع ~15,000 حرف)
# بيطلب عادةً أكتر من كده (شفنا 1133 فعليًا)، فبيترفض فورًا بـ 429
# (rate_limit_exceeded, "exceed the enforced limit") قبل ما يتنفذ خالص.
# بتحديد max_tokens هنا صراحة تحت سقف الموديل (مش القيمة الافتراضية
# المفتوحة)، كل طلب لوحده بيفضل جوه حده المسموح من الأساس، فيشتغل (بنوتس
# مقصوصة لو وصلت الحد، بدل ما يترفض بالكامل). موديلات مش موجودة هنا
# (زي gpt-oss) بتتبعت من غير max_tokens زي ما كانت بالظبط (مفيش سقف OTPM
# بنفس الضيق عندها).
_GROQ_MODEL_MAX_OUTPUT_TOKENS = {
    "qwen/qwen3.8-27b": 900,  # سقف الموديل 1000/دقيقة - هامش أمان 10%
}


def _explain_with_groq_once(text: str, prompt: str = EXPLAIN_PROMPT, model: str = None) -> str:
    """الاستدعاء الخام لـ Groq - مقطع واحد زي ما هو، من غير أي تقسيم."""
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY مش موجود")
    from groq import Groq

    client = Groq(api_key=GROQ_API_KEY)
    model_id = model or GROQ_TEXT_MODEL
    kwargs = {}
    max_tokens = _GROQ_MODEL_MAX_OUTPUT_TOKENS.get(model_id)
    if max_tokens:
        kwargs["max_tokens"] = max_tokens

    completion = client.chat.completions.create(
        model=model_id,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        **kwargs,
    )
    result = (completion.choices[0].message.content or "").strip()
    if not result:
        raise RuntimeError("Groq رجّع نوتس فاضية")
    return result


# حد استباقي لحجم المقطع المبعوت لـ Groq تحديدًا - اتحدد تجريبيًا، مش
# قياس دقيق من Groq نفسها. على عكس التقسيم التكيّفي في
# explain_text_with_split (اللي بيتفعّل بعد ما Groq يرفض الطلب فعلاً)،
# ده بيقسّم *قبل* المحاولة الأولى أصلاً - عرفنا من التجربة إن Groq بيتعب
# مع المقاطع الكبيرة (15,000 حرف) فمفيش داعي نستنى فشل مؤكد كل مرة.
# ثابت احتياطي لو remote_config مش شغالة لأي سبب - القيمة الفعلية
# المستخدمة بترجع من _tuning()["groq_proactive_chunk_limit"].
GROQ_PROACTIVE_CHUNK_LIMIT = 6000

# خريطة provider -> مفتاح الـ API بتاعه، لفلترة أي provider مفيش مفتاح
# ليه من السلاسل قبل حتى ما نحاول - بدل ما نجرب ونفشل بالغلط (وده كان
# بيفتح دايرة الـ circuit breaker بتاعه بالغلط كإنه عطلان مش غير مُفعّل).
_PROVIDER_KEY = {"gemini": GEMINI_API_KEY, "groq": GROQ_API_KEY, "nvidia": NVIDIA_API_KEY}


def _filter_available_providers(chain: list) -> list:
    return [c for c in chain if _PROVIDER_KEY.get(c[0])]


def _explain_with_groq(text: str, prompt: str = EXPLAIN_PROMPT, model: str = None) -> str:
    """زي _explain_with_groq_once، بس لو النص أكبر من الحد الاستباقي،
    بيقسّمه نصين (عند أقرب نهاية جملة) ويجرب كل نص لوحده أولاً - بدل ما
    يستنى Groq يرفضه ويكتشف التقسيم بالتقسيم التكيّفي الأبطأ لاحقًا."""
    if len(text) <= _tuning()["groq_proactive_chunk_limit"]:
        return _explain_with_groq_once(text, prompt, model)

    mid = len(text) // 2
    split_at = text.rfind(". ", 0, mid)
    if split_at == -1:
        split_at = text.rfind(" ", 0, mid)
    if split_at == -1:
        split_at = mid
    first_half = text[:split_at + 1].strip()
    second_half = text[split_at + 1:].strip()

    note_1 = _explain_with_groq(first_half, prompt, model)
    note_2 = _explain_with_groq(second_half, prompt, model)
    return note_1 + "\n\n" + note_2


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
    """نفس فكرة _transcribe_candidates بس لقائمة اختيار موديل التلخيص.

    ترتيب/تفعيل الـ Auto بقى جايين من remote_config.get_summary_chain()
    (ملف model_config.json على الريبو - راجع remote_config.py للتفاصيل:
    remote أولاً، بعدين كاش محلي، بعدين قيم مدمجة في الكود كحماية أخيرة)
    بدل ما يكون array ثابت هنا - عشان نقدر نعدّل الترتيب/نضيف أو نعطّل
    موديل من بعيد من غير إصدار نسخة جديدة من البرنامج.

    الاختيار اليدوي: الموديل المختار بييجي الأول، وبعده باقي السلسلة
    بترتيبها الحرفي زي ما هي في model_config.json - من غير أي تجميع
    لموديلات نفس المزوّد مع بعض (قرار متعمد: ترتيب الملف هو مصدر الحقيقة
    الوحيد للـ fallback، حتى لو موديلات نفس المزوّد مش متجاورة فيه)."""
    default_chain = _filter_available_providers(remote_config.get_summary_chain())

    if SUMMARY_MODEL_CHOICE == "auto":
        return default_chain

    chosen = _resolve_manual_choice(SUMMARY_MODEL_CHOICE, default_chain, _LEGACY_SUMMARY_KEYS)
    if chosen is None:
        # الاختيار مش معروف خالص (موديل اتشال من model_config.json
        # مثلاً، ومش مفتاح قديم نعرفه) - نتعامل معاه كـ Auto.
        return default_chain
    if chosen not in default_chain:
        default_chain = [chosen] + default_chain  # احتياطي: مفيش مفتاحه حاليًا في provider بتاعه

    rest = [c for c in default_chain if c != chosen]
    return [chosen] + rest


def explain_text(text: str, prompt: str = EXPLAIN_PROMPT) -> str:
    """
    بيجرب المرشحين بالترتيب اللي رجعه _summary_candidates() (المختار
    يدويًا الأول لو موجود، وبعده الافتراضي Gemini←Groq كـ fallback).
    لكل محاولة، لو اتأخرت عن وقت متوقع (تقريبي، مبني على طول النص)
    بيتجاوزها للتالي فورًا من غير ما يستناها.
    """
    # تقدير تقريبي جدًا (مش دقيق): ~45 ثانية أساسية + وقت إضافي حسب طول
    # النص - مبني على ملاحظة عملية إن الأجزاء الطويلة بتاخد وقت أطول بكتير.
    timeout_sec = max(45.0, 45 + (len(text) / 1000) * 8)

    candidates = _summary_candidates()
    # شبكة أمان: لو الـ Circuit Breaker قافل كل المرشحين (الدنيا واقعة
    # بالكامل)، أفضل نجرب بدل ما نوقف البرنامج فورًا - بنصفّر كل حاجة
    # ونرجع للقايمة الكاملة بدل الفلترة.
    available = [c for c in candidates if _circuit_available(c[0], c[1])]
    if available:
        candidates = available
    else:
        _circuit_state.clear()

    errors = []

    for provider, model_id in candidates:
        if _cancelled():
            raise _CancelledError()

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
            # بيتحسب من جديد قبل كل محاولة (مش مرة واحدة فوق الدالة) -
            # عشان الوقت المعروض وقت النجاح يبقى وقت المحاولة دي بس،
            # مش شامل وقت أي محاولات فاشلة سابقة لمزوّدين تانيين لنفس
            # المقطع (كان بيدّي أرقام مضلّلة زي "188 ثانية" بدل "23").
            t0 = time.time()
            _log(f"    → بيحاول عبر {label} (ممكن ياخد وقت حسب طول الجزء)...")
            status, result = _run_with_timeout(fn, timeout_sec)

            if status == "ok":
                _log(f"    ✓ خلص عبر {label} ({time.time() - t0:.1f} ثانية)")
                _circuit_record_success(provider, model_id)

                # لو اليوزر مختار موديل تلخيص يدوي، دائرته كانت مفتوحة
                # (واقع)، وموديل تاني من نفس المزوّد بتاعه نجح دلوقتي -
                # نديله فرصة تانية في المقطع الجاي بدل ما يفضل متجنّب
                # لحد ما التهدئة تخلص لوحدها. مقصور على التلخيص اليدوي.
                # لازم نحسب الاختيار اليدوي من غير فلترة الـ circuit breaker
                # (مش من candidates اللي ممكن يبقى اتفلتر منها لو دايرة
                # الاختيار اليدوي نفسه كانت مفتوحة) - وإلا manual_choice
                # هيبقى العنصر الأول المتاح فعليًا مش الاختيار اليدوي
                # الحقيقي، ويبوّظ شرط الـ reset تحت.
                manual_choice = None
                if SUMMARY_MODEL_CHOICE != "auto":
                    _full_chain = _filter_available_providers(remote_config.get_summary_chain())
                    manual_choice = _resolve_manual_choice(SUMMARY_MODEL_CHOICE, _full_chain, _LEGACY_SUMMARY_KEYS)
                if (manual_choice and manual_choice[0] == provider
                        and manual_choice != (provider, model_id)
                        and not _circuit_available(manual_choice[0], manual_choice[1])):
                    _circuit_reset_provider(provider, except_model=model_id)
                    _log(f"    🔁 {manual_choice[1]} هيتجرب تاني في المقطع الجاي (موديل تاني من نفس المزوّد نجح)")

                global LAST_USED_SUMMARY_PROVIDER
                LAST_USED_SUMMARY_PROVIDER = provider
                return result

            if status == "timeout":
                _log(f"    ⚠ {label} اتأخر عن المتوقع (~{timeout_sec:.0f} ثانية) - بيجرب التالي...")
                errors.append(f"{label}: اتأخر عن المتوقع")
                _circuit_record_failure(provider, model_id)
                break

            if _is_structural_otpm_error(result):
                _log(f"    ⚠ {label} سقفه (OTPM) أصغر من حجم المهمة دي بنيويًا - بيتخطاه فورًا (مش rate limit عادي)...")
                errors.append(f"{label}: سقف OTPM بنيوي أصغر من حجم الطلب")
                _circuit_record_failure(provider, model_id)
                break

            if _is_rate_limit_error(result) and attempt < len(RATE_LIMIT_RETRY_DELAYS):
                delay = RATE_LIMIT_RETRY_DELAYS[attempt]
                attempt += 1
                _log(f"    ⏳ {label} وصل لحد الـ rate limit - بيستنى {delay} ثانية ويعيد نفس المزوّد ({attempt}/{len(RATE_LIMIT_RETRY_DELAYS)})...")
                _cancellable_sleep(delay)
                continue

            _log(f"    ⚠ {label} فشل ({friendly_error(result)}), بيجرب التالي...")
            errors.append(f"{label}: {friendly_error(result)}")
            _circuit_record_failure(provider, model_id)
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
            # مفيش حاجة هتتفرّغ فعليًا في المرحلة دي، يعني حلقة التكرار
            # تحت مش هتتنفذ خالص - من غيرها، شريط التقدّم كان بيفضل عارض
            # آخر رقم من عملية سابقة (زي "8/8") بدل ما يتصفّر، وده كان
            # بيظهر كأرقام غريبة ("11/0") لما المرحلة الجاية تبدأ تحدّثه.
            _progress(0, 0, "")

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

                except _CancelledError:
                    # متعامل كفشل عادي (continue للملف التالي) - اتلغت
                    # العملية فعليًا وموقفين هنا، مش بنكمل اللي بعده.
                    _log(f"[⏹] اتلغت العملية - اتفرّغ {i - 1}/{total} قبل الإلغاء، والباقي متجاهل.")
                    break
                except Exception as e:
                    _log(f"[!] تعذر تفريغ {audio_path.name}: {e}")
                    _log("    هيتجاهل الملف ده ويكمل اللي بعده.")
                    continue
                _progress(i, total, "تفريغ الصوت")

                if i < total:
                    try:
                        _cancellable_sleep(CHUNK_PACING_SECONDS)
                    except _CancelledError:
                        _log(f"[⏹] اتلغت العملية - اتفرّغ {i}/{total} قبل الإلغاء، والباقي متجاهل.")
                        break

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


def _split_oversized_sentence(s: str, max_chars: int) -> list:
    """تقسيم "جملة" واحدة (بين علامتين ترقيم، أو سطر كامل من غير أي
    علامات ترقيم خالص) أكبر من max_chars لقطع أصغر، عند أقرب مسافة قبل
    الحد قدر الإمكان - عشان مانقطعش وسط كلمة. لو مفيش أي مسافة مناسبة
    خالص (نادر جدًا، كلمة واحدة ملزّقة طويلة)، بنعمل قص صارم عند الحد
    كملاذ أخير بدل ما نسيب القطعة دي أكبر من الحد المسموح للـ API."""
    pieces = []
    while len(s) > max_chars:
        cut = s.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        piece = s[:cut].strip()
        if piece:
            pieces.append(piece)
        s = s[cut:].strip()
    if s:
        pieces.append(s)
    return pieces


def chunk_text(text: str, max_chars: int = MAX_CHARS_PER_CHUNK):
    """يقسم النص لمقاطع بحد أقصى max_chars، مع محاولة عدم قطع أي جملة نص
    نصين. لو جملة واحدة (أو كل النص - لو مفيش أي علامات ترقيم خالص، زي
    كلام منطوق سريع بلا فواصل) أطول من max_chars لوحدها، بتتقسم هي
    نفسها لقطع أصغر (_split_oversized_sentence) بدل ما تعدّي بحجمها
    الكامل لطلب واحد للـ API (وده كان بيترفض فعليًا عند بعض المزوّدين)."""
    sentences = [s for s in _SENTENCE_END_RE.split(text) if s and s.strip()]
    chunks, current, length = [], [], 0
    for s in sentences:
        if len(s) > max_chars:
            # الجملة نفسها أكبر من الحد - اقفل أي مقطع متجمّع لحد دلوقتي
            # الأول (عشان الترتيب الزمني يفضل صحيح)، وبعدين ضيف القطع
            # المقسومة منها كمقاطع مستقلة.
            if current:
                chunks.append(" ".join(current))
                current, length = [], 0
            chunks.extend(_split_oversized_sentence(s, max_chars))
            continue

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
    build_meeting_consolidation_prompt في prompts.py). لو فشل الدمج (rate
    limit مثلاً)، بيرجع مجرد تجميع بسيط (concatenation) بدل ما يضيع
    المحتوى بالكامل.
    """
    if len(notes_batch) == 1:
        return notes_batch[0]
    combined = "\n\n".join(notes_batch)
    try:
        return explain_text_with_split(combined, consolidation_prompt)
    except _CancelledError:
        # لازم تتفلتر هنا قبل except Exception تحت - وإلا كانت هتتبلع
        # وكإن الدمج "فشل" عادي، فيستمر اللوب اللي فوق يدمج باقي الدفعات
        # من غير ما يوقف فعليًا رغم إن اليوزر دوس "إلغاء".
        raise
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
            if _cancelled():
                raise _CancelledError()
            merged.append(_merge_batch(batch, consolidation_prompt))
            if len(batch) > 1:
                _cancellable_sleep(CHUNK_PACING_SECONDS)
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
          "meeting" = أسلوب "خد نوتس" (توثيق اجتماع)، مبني حسب
          state["meeting_domain"]/["attendees"]/["meeting_extra1"]/
          ["meeting_extra2"] عن طريق build_meeting_prompt.
    """
    is_meeting = mode == "meeting"
    meeting_domain = state.get("meeting_domain", "") if is_meeting else ""
    meeting_attendees = state.get("attendees", "") if is_meeting else ""

    if is_meeting:
        meeting_prompt = build_meeting_prompt(
            meeting_domain, meeting_attendees,
            state.get("meeting_extra1", False), state.get("meeting_extra2", False),
        )
        consolidation_prompt = build_meeting_consolidation_prompt(meeting_domain, meeting_attendees)
    else:
        meeting_prompt = None
        consolidation_prompt = CONSOLIDATION_PROMPT

    def _chunk_prompt(context_carry: str) -> str:
        if is_meeting:
            if not context_carry:
                return meeting_prompt
            # نفس فكرة السياق المختصر بتاعة وضع المحاضرة - بيقلل تكرار
            # نفس المهمة/القرار في "القرارات والمهام" لو المتحدثين رجعوا
            # ذكروها في جزء تاني من نفس الاجتماع.
            return (
                f'سياق (آخر نقطة اتغطت في الجزء اللي قبل ده مباشرة، من نفس '
                f'الاجتماع): "{context_carry}"\n'
                "كمّل من هنا - من غير ما تكرر نفس المهمة أو القرار في قسم "
                '"القرارات والمهام" لو كانت اتوثقت بالفعل هناك.\n\n'
                + meeting_prompt
            )
        return build_explain_prompt(
            state.get("subject", ""),
            state.get("enable_corrections", False),
            state.get("enable_additions", False),
            context_carry=context_carry,
            extra1=state.get("lecture_extra1", False),
            extra2=state.get("lecture_extra2", False),
            extra3=state.get("lecture_extra3", False),
        )

    base_summarized_chars = state["summarized_chars"]
    text_end = len(full_text) if end_chars is None else max(base_summarized_chars, min(end_chars, len(full_text)))
    new_text = full_text[base_summarized_chars:text_end].strip()
    if not new_text:
        _log("[i] مفيش نص جديد يتشرح.")
        _progress(0, 0, "")  # نصفّر شريط التقدّم عشان مايفضلش رقم قديم ظاهر
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
        except _CancelledError:
            _log(f"[⏹] اتلغت العملية - اتشرح {i - 1}/{total} مقطع قبل الإلغاء.")
            stopped_early = True
            break
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
            try:
                _cancellable_sleep(CHUNK_PACING_SECONDS)
            except _CancelledError:
                _log(f"[⏹] اتلغت العملية - اتشرح {i}/{total} مقطع قبل الإلغاء.")
                stopped_early = True
                break

    if not partial_notes:
        return False  # ولا مقطع واحد نجح - مفيش حاجة نحفظها

    if len(partial_notes) == 1:
        final_notes = partial_notes[0]
    else:
        _log("[i] بيجمع نوتس كل المقاطع في نسخة نهائية متماسكة (دمج هرمي على دفعات)...")
        try:
            final_notes = _hierarchical_merge(partial_notes, consolidation_prompt)
        except _CancelledError:
            # المقاطع نفسها خلصت شرحها بنجاح (partial_notes) - الإلغاء حصل
            # في مرحلة "توحيد الصياغة" بس، فمش منطقي نرمي الشغل ده كله.
            # بنحفظهم مجمّعين من غير توحيد نهائي (زي fallback _merge_batch
            # بالظبط لما الدمج يفشل)، ونعلّم العملية كـ"وقفت بدري" عشان
            # الـ checkpoint يتحسب صح.
            _log("[⏹] اتلغت العملية أثناء مرحلة توحيد الصياغة - هنحفظ النوتس الجزئية (من غير دمج نهائي) عشان الشغل اللي خلص ميضيعش.")
            final_notes = "\n\n".join(partial_notes)
            stopped_early = True

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