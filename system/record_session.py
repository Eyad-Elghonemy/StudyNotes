"""
تسجيل صوت النظام (System Audio) بشكل خفيف - بدون أي معالجة وقت التسجيل.
التسجيل بيتقسم أوتوماتيك لملفات صغيرة كل CHUNK_MINUTES دقيقة (بدل ملف واحد
كبير)، عشان يبقى أضمن سواء كانت جلسة قصيرة (كورس) أو طويلة جداً (اجتماع
Teams كذا ساعة) - مفيش داعي تختار نوع الجلسة، التقسيم بيحصل دايماً.

تشغيل: python record_session.py
"""

import queue
import subprocess
import sys
import threading
import time
from datetime import datetime

import numpy as np
import soundcard as sc
import soundfile as sf
from dotenv import load_dotenv

load_dotenv()  # يقرأ ملف .env من نفس مجلد السكريبتات لو موجود

from state_manager import RECORD_FOLDER, pick_lecture_name, compress_to_opus, ffmpeg_available

SAMPLE_RATE = 16000
CHUNK_MINUTES = 30  # كل نص ساعة يتقفل الملف الحالي ويتفتح ملف جديد أوتوماتيك

audio_queue: "queue.Queue[np.ndarray]" = queue.Queue()
stop_flag = threading.Event()


def capture_system_audio():
    default_speaker = sc.default_speaker()
    loopback_mic = sc.get_microphone(
        id=str(default_speaker.name), include_loopback=True
    )
    print(f"[+] بدء التقاط صوت النظام من: {default_speaker.name}")

    with loopback_mic.recorder(samplerate=SAMPLE_RATE, channels=1) as mic:
        while not stop_flag.is_set():
            data = mic.record(numframes=SAMPLE_RATE)
            audio_queue.put(data.flatten().astype(np.float32))


def _compress_in_background(flac_path):
    """يضغط ملف مكتمل لـ Opus في الخلفية من غير ما يوقف التسجيل الحالي."""
    if ffmpeg_available():
        new_path = compress_to_opus(flac_path)
        print(f"[✓] اتضغط: {new_path.name}")
    else:
        print(f"[i] ffmpeg مش متثبت، {flac_path.name} هيفضل FLAC (حجم أكبر).")


def record_worker(lecture: str):
    """
    بيسجل بشكل مستمر، وكل CHUNK_MINUTES دقيقة بيقفل الملف الحالي (ويبدأ
    ضغطه في الخلفية) ويفتح ملف جديد تلقائياً، لحد ما يوصله stop_flag.
    """
    chunk_frames_limit = SAMPLE_RATE * 60 * CHUNK_MINUTES

    def new_path():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return RECORD_FOLDER / f"{lecture}__{ts}.flac"

    current_path = new_path()
    frames_written = 0
    print(f"[+] التسجيل هيتحفظ في: {current_path.name}")

    f = sf.SoundFile(str(current_path), mode="w", samplerate=SAMPLE_RATE, channels=1, format="FLAC")
    try:
        while not stop_flag.is_set() or not audio_queue.empty():
            try:
                chunk = audio_queue.get(timeout=1)
            except queue.Empty:
                continue

            f.write(chunk)
            frames_written += len(chunk)

            # وصلنا لحد الوقت المحدد للجزء ده؟ اقفله وابدأ جزء جديد
            if frames_written >= chunk_frames_limit and not stop_flag.is_set():
                f.close()
                threading.Thread(
                    target=_compress_in_background, args=(current_path,), daemon=True
                ).start()

                current_path = new_path()
                frames_written = 0
                print(f"[+] جزء جديد بدأ (بعد {CHUNK_MINUTES} دقيقة): {current_path.name}")
                f = sf.SoundFile(
                    str(current_path), mode="w", samplerate=SAMPLE_RATE,
                    channels=1, format="FLAC",
                )
    except Exception as e:
        print(f"[!] تحذير: حصلت مشكلة أثناء الكتابة: {e}")
    finally:
        # لازم نتأكد من قفل الملف دايماً حتى لو حصل استثناء نص الكتابة
        # (زي امتلاء الديسك) - وإلا الملف بيفضل مفتوح/مقفول من نظام
        # التشغيل ومينفعش يتقرأ أو يتضغط بعد كده.
        try:
            f.close()
        except Exception:
            pass
        print(f"[✓] آخر جزء اتحفظ: {current_path.name}")
        # نضغط آخر جزء برضه (بشكل متزامن هنا عشان نستناه قبل ما نكمل)
        _compress_in_background(current_path)


def main():
    lecture = pick_lecture_name()
    print(f"\n[i] المحاضرة المختارة: {lecture}")

    capture_thread = threading.Thread(target=capture_system_audio, daemon=True)
    write_thread = threading.Thread(target=record_worker, args=(lecture,), daemon=True)
    capture_thread.start()
    write_thread.start()

    print(f"[i] التسجيل هيتقسم أوتوماتيك كل {CHUNK_MINUTES} دقيقة.")
    print("[i] شغّل الفيديو/الاجتماع دلوقتي. اضغط Ctrl+C لما توقف.")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[!] جاري إيقاف التسجيل...")
        stop_flag.set()
        # نستنى فعلياً لحد ما الثريد يقفل الجزء الأخير ويضغطه، بدل تخمين
        # وقت ثابت ممكن يكون أقصر من اللازم (يبوظ آخر جزء) أو أطول من
        # اللازم من غير داعي.
        write_thread.join(timeout=60)

    answer = input(
        "\nعايز تفريغ وتلخيص للي اتفرجت عليه لحد دلوقتي؟ (y/n): "
    ).strip().lower()

    if answer == "y":
        print("\n[i] جاري التفريغ والتلخيص...\n")
        subprocess.run([sys.executable, "process_lecture.py", lecture])
    else:
        print(f"\n[i] تمام، لما تكمل المحاضرة دي تاني، اختار \"{lecture}\" من القائمة.")


if __name__ == "__main__":
    main()