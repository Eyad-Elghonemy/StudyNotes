"""
إعدادات سلاسل الموديلات (ترتيب/تفعيل/تعطيل التفريغ والتلخيص + أرقام الـ
circuit breaker) من ملف JSON عام على الريبو، عشان نقدر نعدّلها من بعيد
من غير ما نضطر نصدر نسخة جديدة من البرنامج لكل تغيير بسيط في الترتيب.

الترتيب في اختيار المصدر الفعلي (get_effective_config):
  1) الملف البعيد لو نجح الاتصال واتعدّى الـ validation
  2) آخر نسخة محفوظة محليًا (كاش) لو موجودة
  3) القيم المدمجة في الكود (HARDCODED_DEFAULT) - شبكة أمان أخيرة، البرنامج
     لازم يشتغل عادي تمامًا حتى لو الملف البعيد اتمسح أو الريبو اتقفل

الفحص بيحصل مرة واحدة بس عند فتح البرنامج (مش قبل كل عملية تلخيص) - زي
نفس فكرة updater.py بالظبط - عشان ميضيفش زمن شبكة على كل عملية فعلية.
"""

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from state_manager import BASE_DIR

CONFIG_URL = (
    "https://raw.githubusercontent.com/Eyad-Elghonemy/StudyNotes/main/model_config.json"
)
_HEADERS = {"User-Agent": "StudyNotes-remote-config"}
_CACHE_PATH = BASE_DIR / ".state" / "remote_config_cache.json"

_VALID_PROVIDERS = {"gemini", "groq", "nvidia"}
_MAX_CHAIN_LEN = 20

# نفس القيم الحالية في process_lecture.py بالظبط - لو الملف البعيد مش
# موجود أصلاً (أول تشغيل، مفيش نت) والكاش فاضي، البرنامج يفضل يشتغل
# بنفس السلوك اللي كان عليه قبل الفيتشر ده خالص.
HARDCODED_DEFAULT = {
    "version": 1,
    "summary_chain": [
        {"provider": "gemini", "model": "gemini-3.8-flash", "enabled": True},
        {"provider": "gemini", "model": "gemini-3.7-flash", "enabled": True},
        {"provider": "groq", "model": "qwen/qwen3.8-27b", "enabled": True},
        {"provider": "gemini", "model": "gemini-3.6-flash", "enabled": True},
        {"provider": "groq", "model": "openai/gpt-oss-120b", "enabled": True},
        {"provider": "nvidia", "model": "nvidia/nemotron-3-ultra-550b-a55b", "enabled": True},
        {"provider": "nvidia", "model": "nvidia/nemotron-3.5-lightning-30b-a3b", "enabled": True},
        {"provider": "groq", "model": "openai/gpt-oss-20b", "enabled": True},
        {"provider": "gemini", "model": "gemini-3.5-flash-lite", "enabled": True},
    ],
    "transcribe_chain": [
        {"provider": "groq", "model": "whisper-large-v3", "enabled": True},
        {"provider": "groq", "model": "whisper-large-v3-turbo", "enabled": True},
        {"provider": "gemini", "model": None, "enabled": True},
    ],
    "tuning": {
        "circuit_failure_threshold": 2,
        "circuit_base_cooldown": 90,
        "circuit_max_cooldown": 600,
        "groq_proactive_chunk_limit": 6000,
    },
}

_TUNING_BOUNDS = {
    "circuit_failure_threshold": (1, 10),
    "circuit_base_cooldown": (10, 3600),
    "circuit_max_cooldown": (10, 3600),
    "groq_proactive_chunk_limit": (1000, 50000),
}

# بيتملى مرة واحدة وقت فتح البرنامج (أو lazy عند أول استخدام لو حد نادى
# الدوال دي قبل ما حد يستدعي refresh() صراحة) - مش بيتجدد كل عملية.
_effective_config: dict | None = None


def _validate_chain(raw_chain, field_name: str) -> list:
    """بيرجع ليستة العناصر الصحيحة بس (مش بيرفض السلسلة كلها لعنصر غلط
    واحد) - وبيستبعد العناصر enabled=false. لو مفيش عنصر صحيح واحد
    بعد الفلترة، بيرجع ليستة فاضية (اللي استدعى الدالة هو اللي بيقرر
    وقتها يرجع للكاش/الافتراضي بدل سلسلة فاضية فعليًا)."""
    if not isinstance(raw_chain, list) or len(raw_chain) > _MAX_CHAIN_LEN:
        return []

    result = []
    for item in raw_chain:
        try:
            if not isinstance(item, dict):
                continue
            if not item.get("enabled", True):
                continue
            provider = item.get("provider")
            if provider not in _VALID_PROVIDERS:
                continue
            model = item.get("model")
            if model is not None and not isinstance(model, str):
                continue
            if isinstance(model, str) and not model.strip():
                continue
            result.append((provider, model))
        except Exception:
            continue
    return result


def _validate_tuning(raw_tuning) -> dict:
    """بيرجع dict فيه بس المفاتيح المعروفة واللي قيمتها جوه حدودها
    المنطقية - أي مفتاح غلط أو قيمة خارج الحدود بيتجاهل وترجع القيمة
    الافتراضية بتاعته بدل ما توقف الملف كله."""
    out = dict(HARDCODED_DEFAULT["tuning"])
    if not isinstance(raw_tuning, dict):
        return out
    for key, (lo, hi) in _TUNING_BOUNDS.items():
        val = raw_tuning.get(key)
        if isinstance(val, (int, float)) and lo <= val <= hi:
            out[key] = val
    return out


def _validate_config(data: dict) -> dict | None:
    """يرجع dict نضيف (chains متحققة + tuning متحقق)، أو None لو الملف
    مش قابل للاستخدام خالص (مش JSON object، أو السلسلتين فاضيتين مع
    بعض بعد الفلترة)."""
    if not isinstance(data, dict):
        return None

    summary_chain = _validate_chain(data.get("summary_chain"), "summary_chain")
    transcribe_chain = _validate_chain(data.get("transcribe_chain"), "transcribe_chain")

    # لو السلسلتين فاضيتين مع بعض، الملف مفيدش حاجة - ارفضه كله.
    # لو واحدة بس فاضية، نسيب الفاضية دي تتعوض بالـ hardcoded تحت
    # (أفضل من ما نرفض ملف فيه سلسلة صحيحة عشان التانية غلط).
    if not summary_chain and not transcribe_chain:
        return None

    return {
        "version": data.get("version", 1),
        "summary_chain": summary_chain or [
            (c["provider"], c["model"]) for c in HARDCODED_DEFAULT["summary_chain"]
        ],
        "transcribe_chain": transcribe_chain or [
            (c["provider"], c["model"]) for c in HARDCODED_DEFAULT["transcribe_chain"]
        ],
        "tuning": _validate_tuning(data.get("tuning")),
    }


def _hardcoded_as_tuples() -> dict:
    return {
        "version": HARDCODED_DEFAULT["version"],
        "summary_chain": [(c["provider"], c["model"]) for c in HARDCODED_DEFAULT["summary_chain"]],
        "transcribe_chain": [(c["provider"], c["model"]) for c in HARDCODED_DEFAULT["transcribe_chain"]],
        "tuning": dict(HARDCODED_DEFAULT["tuning"]),
    }


def _fetch_remote(timeout: float = 5.0) -> dict | None:
    try:
        req = urllib.request.Request(CONFIG_URL, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, ValueError):
        return None
    return _validate_config(raw)


def _load_cache() -> dict | None:
    try:
        raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    return _validate_config(raw)


def _save_cache(raw_data: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(raw_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass  # فشل حفظ الكاش مش لازم يوقف أي حاجة - المرة الجاية هيحاول تاني


def refresh(timeout: float = 5.0) -> str:
    """بتتنادى مرة واحدة عند فتح البرنامج (من ثريد منفصل عشان متعطلش
    فتح الواجهة). بترجع "remote" / "cache" / "default" عشان الواجهة
    تقدر - لو عايزة - تعرض مصدر الإعدادات الحالي.
    """
    global _effective_config

    try:
        req = urllib.request.Request(CONFIG_URL, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, ValueError):
        raw = None

    if raw is not None:
        validated = _validate_config(raw)
        if validated is not None:
            _effective_config = validated
            _save_cache(raw)  # بنخزن الخام نفسه - الـ validation بيتكرر وقت القراءة برضو
            return "remote"

    cached = _load_cache()
    if cached is not None:
        _effective_config = cached
        return "cache"

    _effective_config = _hardcoded_as_tuples()
    return "default"


def get_summary_chain() -> list:
    if _effective_config is None:
        refresh()
    return list(_effective_config["summary_chain"])


def get_transcribe_chain() -> list:
    if _effective_config is None:
        refresh()
    return list(_effective_config["transcribe_chain"])


def get_tuning() -> dict:
    if _effective_config is None:
        refresh()
    return dict(_effective_config["tuning"])
