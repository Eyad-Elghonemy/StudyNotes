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
import threading
import time
from pathlib import Path

# المسار الأساسي للمشروع: قابل للتخصيص عن طريق متغير بيئة STUDYNOTES_DIR
# (تقدر تحطه في ملف .env)، ولو مش موجود بيستخدم فولدر جنب السكريبتات نفسها
# عشان المشروع يشتغل من غير ما تعدل أي مسار يدوياً على أي جهاز.
BASE_DIR = Path(os.environ.get("STUDYNOTES_DIR", Path(__file__).resolve().parent))
RECORD_FOLDER = BASE_DIR / "Sound_Recorded"
TRANSCRIPT_FOLDER = BASE_DIR / "Transcript"
MARKDOWN_FOLDER = BASE_DIR / "Markdown"
STATE_FOLDER = BASE_DIR / ".state"

for folder in (RECORD_FOLDER, TRANSCRIPT_FOLDER, MARKDOWN_FOLDER, STATE_FOLDER):
    folder.mkdir(parents=True, exist_ok=True)


def safe_name(name: str) -> str:
    """تنضيف اسم المحاضرة من رموز ممنوعة في أسماء الملفات على ويندوز."""
    cleaned = "".join(c for c in name if c not in r'\/:*?"<>|').strip()
    return cleaned or "untitled_lecture"


def list_existing_lectures() -> list[str]:
    """
    يرجع أسماء كل المحاضرات المعروفة، سواء كان ليها ملف حالة (بعد أول
    تفريغ ناجح) أو لسه بس ملفات صوت مسجلة.
    """
    from_state = {p.stem for p in STATE_FOLDER.glob("*.json")}
    from_audio = set()
    for ext in ("opus", "flac", "wav"):
        for p in RECORD_FOLDER.glob(f"*__*.{ext}"):
            name = p.stem.rsplit("__", 1)[0]
            from_audio.add(name)
    return sorted(from_state | from_audio)


def load_state(lecture: str) -> dict:
    path = STATE_FOLDER / f"{lecture}.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}
    # قيم افتراضية لأي حقل جديد اتضاف بعدين، عشان الملفات القديمة تفضل شغالة
    data.setdefault("transcribed_files", [])  # اتفرغت (بس مش بالضرورة اتشرحت)
    data.setdefault("explained_files", [])    # اتفرغت واتشرحت كمان
    data.setdefault("summarized_chars", 0)    # لحد أي حرف في الترانسكريبت اتشرح
    # مكان كل ملف صوت جوه ملف الترانسكريبت التراكمي: {filename: [start, end]}
    # (بالحروف مش البايتات) - بيتسجل وقت التفريغ في process_lecture.py،
    # ومستخدم عشان نقدر نمسح تفريغ ملف واحد بعينه من غير ما نبوظ الباقي.
    data.setdefault("transcript_ranges", {})
    return data


def save_state(lecture: str, state: dict) -> None:
    path = STATE_FOLDER / f"{lecture}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


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
    لو ffmpeg مش موجود، أو الضغط فشل، بيرجع نفس المسار الأصلي.

    بيتأكد فعلياً إن الملف الأصلي اتمسح بنجاح (مع إعادة محاولة قصيرة لو
    كان لسه مقفول من عملية تانية)، عشان مايفضلش نسختين من نفس الجزء
    (FLAC + Opus) بيتفرغوا مرتين بالغلط.
    """
    if not ffmpeg_available():
        return src_path

    out_path = src_path.with_suffix(".opus")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(src_path),
                "-ar", "16000", "-ac", "1",
                "-c:a", "libopus", "-b:a", bitrate,
                str(out_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not out_path.exists() or out_path.stat().st_size == 0:
            return src_path  # الضغط فشل فعلياً رغم مفيش exception

        # نحاول نمسح الأصلي كذا مرة لو كان لسه مقفول من ثريد تاني
        for attempt in range(5):
            try:
                src_path.unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.5)
        return out_path
    except Exception:
        return src_path


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


def list_lecture_chunks(lecture: str, state: dict) -> list[dict]:
    """
    يرجع كل جزء صوتي للمحاضرة دي، مع حالته وطوله وحجمه. لو نفس الجزء
    (نفس الـ timestamp) موجود بصيغتين (FLAC وOpus) بسبب باج قديم، بياخد
    الـ Opus بس ويتجاهل الـ FLAC المكرر عشان يمنع التفريغ المزدوج.

    "corrupted": True معناها المدة اللي جاية من الملف نفسه غير منطقية
    (غالبًا الملف اتقفل بشكل غير سليم) - الأجزاء دي بيتم استبعادها من أي
    إجمالي (مدة/تقدير توكينز) في الواجهة، وميتحطش تحديدها تلقائي.
    """
    by_stem_prefix = {}  # timestamp -> path (بالأولوية لـ opus)
    for ext, priority in (("opus", 0), ("flac", 1), ("wav", 2)):
        for p in RECORD_FOLDER.glob(f"{lecture}__*.{ext}"):
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
            for p in RECORD_FOLDER.glob(f"{lecture}__*.{ext}"):
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