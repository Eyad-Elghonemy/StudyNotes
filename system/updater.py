"""
آلية التحقق من وجود نسخة أحدث من StudyNotes، وتنزيلها وتشغيل تثبيتها
(اليوزر بيكمّل التثبيت بنفسه بضغطات بسيطة Next/Install).

الفكرة بالكامل:
1) check_for_update(current_version) بتقرا ملف version.json ثابت من
   جذر الريبو على GitHub (عن طريق raw.githubusercontent.com، مش GitHub
   REST API) وتقارن رقم النسخة فيه بالنسخة الحالية اللي البرنامج شغال
   بيها.
2) لو فيه نسخة أحدث، بترجع معلومات عنها (رقم النسخة + رابط ملف التثبيت
   + ملاحظات النسخة لو موجودة).
3) لو اليوزر وافق يحدّث، download_installer() بتنزل ملف التثبيت (Setup.exe)
   لفولدر مؤقت على جهازه، مع عرض تقدّم التحميل.
4) launch_installer() بتشغّل ملف التثبيت ده عادي (زي أي تثبيت يدوي) -
   هتظهر نافذة Inno Setup المعتادة واليوزر بيكمّلها بنفسه.

ليه version.json بدل GitHub API مباشرة (Bug #13 في تقرير المراجعة):
الـ REST API (api.github.com) بيحدد الطلبات الـ unauthenticated بـ 60
طلب/ساعة *لكل IP عام*، مش لكل يوزر. في شبكات بتستخدم CGNAT (شائع جدًا
عند بعض مزوّدي خدمة الإنترنت في مصر مثلاً)، مئات/آلاف الأجهزة بتظهر
بنفس الـ IP - فالحد ده ممكن يتخلّص بسرعة من استخدام ناس تانيين خالص على
نفس الشبكة، والبرنامج (زي ما كان مكتوب قبل كده) بيترجم فشل الطلب ده
لـ"مفيش تحديث" بصمت، من غير ما اليوزر يعرف إن الفحص نفسه فشل أصلاً.
raw.githubusercontent.com (قراءة ملف ثابت، مش REST API) مالهاش نفس
الحد الصارم ده - نفس الفكرة اللي remote_config.py شغالة بيها بالظبط
لملف model_config.json.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# ============================================================================
# غيّر الاتنين دول لو الريبو اتنقل أو اتغيّر اسمه يومًا
# ============================================================================
GITHUB_OWNER = "Eyad-Elghonemy"
GITHUB_REPO = "StudyNotes"

_VERSION_URL = (
    f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main/version.json"
)
_RELEASES_PAGE = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

_HEADERS = {"User-Agent": f"{GITHUB_REPO}-update-checker"}


def _parse_version(v: str) -> tuple[int, ...]:
    """يحوّل 'v1.5.2' أو '1.5.2' لـ (1, 5, 2) عشان المقارنة تبقى رقمية
    صح (يعني 1.10.0 أكبر من 1.9.0)، مش مقارنة نصوص عادية."""
    v = v.strip().lstrip("vV")
    parts = []
    for chunk in v.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def check_for_update(current_version: str, timeout: int = 8) -> dict | None:
    """
    بترجع None في الحالات دي (كلها "مفيش تحديث" من وجهة نظر البرنامج،
    من غير ما توقف أو توريه رسالة خطأ لليوزر - مشكلة نت مؤقتة مثلاً
    مفيهاش داعي نزعجه بيها):
    - مفيش نت / الملف مش موجود/وصل بـ JSON غلط / أي خطأ تاني غير متوقع
    - version.json مفيهوش download_url أصلاً
    - النسخة الحالية هي الأحدث فعلاً

    لو فيه تحديث فعلي، بترجع dict فيه:
    {"version": "1.5.0", "download_url": "...", "notes": "..."}
    """
    try:
        req = urllib.request.Request(_VERSION_URL, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None

    latest = data.get("version", "")
    if not latest or not is_newer(latest, current_version):
        return None

    download_url = data.get("download_url")
    if not download_url:
        # version.json اتعدّل بالغلط من غير download_url - نتجاهله
        # (بدل ما نودي اليوزر لرابط فاضي)
        return None

    return {
        "version": latest.lstrip("vV"),
        "download_url": download_url,
        "notes": (data.get("notes") or "").strip(),
    }


def download_installer(download_url: str, on_progress=None, version: str | None = None) -> Path:
    """
    بتنزل ملف التثبيت لفولدر مؤقت على الجهاز وترجع مساره.
    on_progress (اختياري): دالة بتتنادى بنسبة مئوية (0-100) أثناء التنزيل،
    لو عايز تعرض progress bar لليوزر.
    version (اختياري): رقم النسخة الجديدة، بيتحط في اسم الملف عشان لو
    محاولة تحديث سابقة فشلت في النص وسابت ملف قديم/جزئي بنفس الاسم في
    فولدر Temp، المحاولة الجديدة متتعارضش معاه (Bug #14 في تقرير
    المراجعة). لو مش موصّلة (أو فيها رموز غريبة)، بيرجع للاسم الثابت
    القديم زي ما هو - مفيش داعي نوقف التحميل بسبب ده.
    """
    safe_version = re.sub(r"[^A-Za-z0-9._-]", "", version or "")
    suffix = f"_{safe_version}" if safe_version else ""
    dest = Path(tempfile.gettempdir()) / f"StudyNotes_Update_Setup{suffix}.exe"
    # بنكتب على اسم مؤقت وبنعمل rename لحظي (atomic) للاسم النهائي بس
    # بعد ما التحميل يخلص بالكامل - لو النت اتقطع في النص (انقطاع، خطأ
    # شبكة، أي استثناء أثناء القراءة/الكتابة)، الملف الجزئي بيفضل جوه
    # .tmp بس، ومفيش أي لحظة فيها ملف ناقص موجود على الاسم النهائي
    # (BUG-06 في التقرير الجنائي) اللي launch_installer() ممكن تحاول
    # تشغّله أو محاولة تحميل تانية تتلخبط معاه.
    tmp_dest = dest.with_suffix(dest.suffix + ".tmp")

    req = urllib.request.Request(download_url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        try:
            with open(tmp_dest, "wb") as f:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if on_progress and total:
                        on_progress(int(downloaded * 100 / total))
        except Exception:
            tmp_dest.unlink(missing_ok=True)
            raise

    os.replace(tmp_dest, dest)
    return dest


def launch_installer(installer_path: Path) -> None:
    """
    بتشغّل ملف التثبيت عادي (زي ما لو اليوزر دبل كليك عليه بنفسه) - هتظهر
    نافذة Inno Setup المعتادة (Next / Next / Install)، واليوزر بيكمّلها
    بنفسه.

    اتغيّرت من تثبيت صامت تمامًا (/VERYSILENT) للشكل العادي ده عمدًا:
    التثبيت الصامت كان بيفشل بصمت في حالات كتير مختلفة حسب جهاز اليوزر
    (مضاد الفيروسات، صلاحيات، تعارضات...) من غير ما نقدر نعرف السبب أو
    نوريله رسالة خطأ مفيدة - التثبيت العادي أبسط وأضمن، وبيوري أي مشكلة
    بوضوح لو حصلت بدل ما تفشل في الخفاء.

    CloseApplications=yes في ملف الـ .iss لسه بيتكفّل بقفل أي نسخة شغالة
    من StudyNotes تلقائيًا لو لقاها وقت التثبيت، فمفيش داعي لأي علامة
    هنا لقفل البرنامج - ده بيحصل تلقائي من جوه المثبّت نفسه.
    """
    subprocess.Popen([str(installer_path)], close_fds=True)