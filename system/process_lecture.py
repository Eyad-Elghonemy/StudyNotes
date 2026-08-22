"""
يفرّغ ملفات صوت لمحاضرة معينة (Groq الأول للسرعة، Gemini كبديل)، يضيفها
لملف الترانسكريبت التراكمي (بيتحفظ دايماً كنسخة احتياطية خام)، وبعدين
يحوّل الجزء الجديد لنوتس مركزة بأسلوب المحاضر (Gemini الأول، Groq كبديل).

تشغيل: python process_lecture.py "اسم المحاضرة"
"""

import os
import re
import sys
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
)

load_dotenv()

_log_callback = print
_progress_callback = None


def set_logger(callback):
    global _log_callback
    _log_callback = callback


def set_progress_callback(callback):
    global _progress_callback
    _progress_callback = callback


def _log(msg: str):
    _log_callback(msg)


def _progress(done: int, total: int, label: str = ""):
    if _progress_callback:
        _progress_callback(done, total, label)


# ------------------ إعدادات المزوّدين ------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-3.6-flash"

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_TRANSCRIBE_MODEL = "whisper-large-v3"
GROQ_TEXT_MODEL = "llama-3.3-70b-versatile"

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

**الأهم: لازم تبرز أي حاجة المحاضر شدّد عليها أو كررها أو نبّه عليها،**
باستخدام الصناديق دي بالظبط (Markdown blockquote):

- لو المحاضر قال حاجة مهمة بشكل واضح (زي "ده مهم جداً"، أو كرر نفس
  الفكرة أكتر من مرة في نفس الجزء):
  > 💡 **مهم:** [النقطة اللي أكد عليها]

- لو المحاضر أشار إنها سؤال إنترفيو محتمل (زي "هيسألوك في الإنترفيو
  عن كذا"، أو "دي حاجة بتتسأل كتير"، أو "لازم تعرفها للإنترفيوهات"):
  > 🎯 **سؤال إنترفيو محتمل:** [السؤال أو النقطة]

- لو المحاضر نبّه على حاجة (تحذير، خطأ شائع، حاجة الطلبة بينسوها):
  > ⚠️ **تنبيه:** [النقطة]

استخدم الصناديق دي بس لما فعلاً يكون فيه إشارة واضحة من المحاضر في الكلام
نفسه، متخترعش نقط مهمة من عندك. خلي كل حاجة مختصرة ومركزة - الهدف نوتس
للمراجعة السريعة، مش توثيق شامل. متكتبش أي مقدمة أو خاتمة عامة، ادخل في
النقط على طول."""


# =========================================================================
# طبقة الاتصال بالنماذج
# =========================================================================

def _gemini_client() -> "genai.Client":
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY مش موجود")
    return genai.Client(api_key=GEMINI_API_KEY)


def _transcribe_with_gemini(audio_path) -> str:
    client = _gemini_client()
    uploaded = client.files.upload(file=str(audio_path))
    while uploaded.state.name == "PROCESSING":
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)
    if uploaded.state.name == "FAILED":
        raise RuntimeError("فشل رفع الملف الصوتي لـ Gemini")

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[TRANSCRIBE_PROMPT, uploaded],
    )
    return (response.text or "").strip()


def _transcribe_with_groq(audio_path) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY مش موجود")
    from groq import Groq

    client = Groq(api_key=GROQ_API_KEY)
    with open(audio_path, "rb") as f:
        result = client.audio.transcriptions.create(
            file=(str(audio_path), f.read()),
            model=GROQ_TRANSCRIBE_MODEL,
            language="ar",
            response_format="text",
        )
    return str(result).strip()


def transcribe_audio_file(audio_path) -> str:
    """Groq الأول (أسرع بكتير)، Gemini كبديل لو Groq فشل أو مش متاح.
    لو حجم الملف أكبر من حد Groq، بنتخطى المحاولة ونروح لـ Gemini على طول."""
    t0 = time.time()

    try:
        size_bytes = Path(audio_path).stat().st_size
    except Exception:
        size_bytes = 0

    if size_bytes > GROQ_MAX_FILE_BYTES:
        size_mb = size_bytes / (1024 * 1024)
        _log(
            f"    ⚠ حجم الملف {size_mb:.1f}MB أكبر من حد Groq (25MB)، "
            f"هيتبعت لـ Gemini على طول من غير ما نجرب Groq."
        )
        try:
            text = _transcribe_with_gemini(audio_path)
            _log(f"    ✓ خلص عبر Gemini ({time.time() - t0:.1f} ثانية)")
            return text
        except Exception as e2:
            raise RuntimeError(f"فشل التفريغ عبر Gemini (والملف أكبر من حد Groq): {e2}")

    try:
        _log("    → بيحاول عبر Groq...")
        text = _transcribe_with_groq(audio_path)
        _log(f"    ✓ خلص عبر Groq ({time.time() - t0:.1f} ثانية)")
        return text
    except Exception as e:
        _log(f"    ⚠ Groq فشل ({e}), بيجرب Gemini كبديل...")
        try:
            text = _transcribe_with_gemini(audio_path)
            _log(f"    ✓ خلص عبر Gemini ({time.time() - t0:.1f} ثانية)")
            return text
        except Exception as e2:
            raise RuntimeError(f"فشل التفريغ بالاتنين. Groq: {e} | Gemini: {e2}")


def _explain_with_gemini(text: str) -> str:
    client = _gemini_client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=text,
        config={"system_instruction": EXPLAIN_PROMPT},
    )
    return response.text


def _explain_with_groq(text: str) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY مش موجود")
    from groq import Groq

    client = Groq(api_key=GROQ_API_KEY)
    completion = client.chat.completions.create(
        model=GROQ_TEXT_MODEL,
        messages=[
            {"role": "system", "content": EXPLAIN_PROMPT},
            {"role": "user", "content": text},
        ],
    )
    return completion.choices[0].message.content


def explain_text(text: str) -> str:
    """Gemini الأول (جودة شرح أعلى)، Groq كبديل لو فشل."""
    t0 = time.time()
    try:
        _log("    → بيحاول عبر Gemini (ممكن ياخد وقت حسب طول الجزء)...")
        result = _explain_with_gemini(text)
        _log(f"    ✓ خلص عبر Gemini ({time.time() - t0:.1f} ثانية)")
        return result
    except Exception as e:
        _log(f"    ⚠ Gemini فشل ({e}), بيجرب Groq كبديل...")
        try:
            result = _explain_with_groq(text)
            _log(f"    ✓ خلص عبر Groq ({time.time() - t0:.1f} ثانية)")
            return result
        except Exception as e2:
            raise RuntimeError(f"فشل الشرح بالاتنين. Gemini: {e} | Groq: {e2}")


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


def summarize_new_part(lecture: str, full_text: str, state: dict) -> None:
    """
    يحوّل الجزء الجديد من النص لنوتس (اللي بعد آخر نقطة اتشرحت)،
    ويضيفه كقسم جديد في ملف الـ Markdown بتاريخ اليوم.
    """
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
        _log(f"    [i] مقطع {i}/{total} ...")
        _progress(i - 1, total, "تحويل لنوتس")
        partial_notes.append(explain_text(c))
        _progress(i, total, "تحويل لنوتس")

    if len(partial_notes) == 1:
        final_notes = partial_notes[0]
    else:
        _log("[i] بيجمع نوتس كل المقاطع في نسخة نهائية متماسكة...")
        combined = "\n\n".join(partial_notes)
        final_notes = explain_text(combined)

    md_path = MARKDOWN_FOLDER / f"{lecture}.md"
    today = datetime.now().strftime("%Y-%m-%d %H:%M")

    header = f"# {lecture}\n\n" if not md_path.exists() else ""
    section = f"{header}## تحديث - {today}\n\n{final_notes}\n\n---\n\n"

    with open(md_path, "a", encoding="utf-8") as f:
        f.write(section)

    # نحفظ نسخة من الحالة قبل التحديث عشان نقدر نلغيه (Undo) لو النتيجة
    # طلعت وحشة أو فيها هلوسة
    state["_undo"] = {
        "summarized_chars": state["summarized_chars"],
        "explained_files": list(state["explained_files"]),
    }
    state["summarized_chars"] = len(full_text)
    state["explained_files"].extend(files_about_to_be_explained)
    with get_lecture_lock(lecture):
        save_state(lecture, state)

    _log(f"[✓] النوتس الجديدة اتضافت في: {md_path}")


def undo_last_notes_update(lecture: str) -> bool:
    """
    يلغي آخر تحديث نوتس: يشيل آخر قسم "## تحديث - ..." من ملف الـ
    Markdown، ويرجّع حالة التتبع (summarized_chars وexplained_files)
    لنفس ما كانت عليه قبل التحديث ده، عشان تقدر تعيد المحاولة أو تسيبه.
    بيرجع True لو نجح، False لو مفيش تحديث يتلغى.
    """
    state = load_state(lecture)
    backup = state.get("_undo")
    md_path = MARKDOWN_FOLDER / f"{lecture}.md"

    if not backup or not md_path.exists():
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

    state["summarized_chars"] = backup["summarized_chars"]
    state["explained_files"] = backup["explained_files"]
    state.pop("_undo", None)
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