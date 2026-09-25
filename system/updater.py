"""
آلية التحقق من وجود نسخة أحدث من StudyNotes على GitHub Releases، وتنزيلها
وتثبيتها صامتة (من غير أي تفاعل تقني من اليوزر) لو وافق.

الفكرة بالكامل:
1) check_for_update(current_version) بتسأل GitHub API: "إيه آخر Release
   منزّل على الريبو؟" وتقارنه بالنسخة الحالية اللي البرنامج شغال بيها.
2) لو فيه نسخة أحدث، بترجع معلومات عنها (رقم النسخة + رابط ملف التثبيت
   المرفق بالـ Release + ملاحظات النسخة لو موجودة).
3) لو اليوزر وافق يحدّث، download_installer() بتنزل ملف التثبيت (Setup.exe)
   لفولدر مؤقت على جهازه.
4) launch_silent_installer() بتشغّل ملف التثبيت ده بعلامات "صامتة" (من
   غير ما تظهرله أي نافذة تثبيت خالص)، وبتسكّر البرنامج الحالي عشان
   ملفاته تبقى حرة يتكتب عليها.

ملحوظة مهمة: العلامات الصامتة دي (/VERYSILENT...) خاصة بـ Inno Setup -
لازم ملف الـ Setup.exe يكون معمول بيه فعلاً (وده هيحصل في خطوة لاحقة)
عشان الآلية دي تشتغل فعليًا من الأول للآخر.
"""

import json
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

_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
_RELEASES_PAGE = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

# GitHub API بيرفض أي طلب من غير User-Agent واضح
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
    - مفيش نت / GitHub مش راضي يرد / أي خطأ تاني غير متوقع
    - آخر Release مفيهوش ملف .exe مرفق أصلاً
    - النسخة الحالية هي الأحدث فعلاً

    لو فيه تحديث فعلي، بترجع dict فيه:
    {"version": "1.5.0", "download_url": "...", "notes": "..."}
    """
    try:
        req = urllib.request.Request(_API_URL, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None

    latest_tag = data.get("tag_name", "")
    if not latest_tag or not is_newer(latest_tag, current_version):
        return None

    download_url = None
    for asset in data.get("assets", []):
        name = asset.get("name", "")
        if name.lower().endswith(".exe"):
            download_url = asset.get("browser_download_url")
            break

    if not download_url:
        # فيه Release أحدث بس ملف الـ exe مش متحط عليه لسه - نتجاهله
        # (بدل ما نودي اليوزر لصفحة فاضية)
        return None

    return {
        "version": latest_tag.lstrip("vV"),
        "download_url": download_url,
        "notes": (data.get("body") or "").strip(),
    }


def download_installer(download_url: str, on_progress=None) -> Path:
    """
    بتنزل ملف التثبيت لفولدر مؤقت على الجهاز وترجع مساره.
    on_progress (اختياري): دالة بتتنادى بنسبة مئوية (0-100) أثناء التنزيل،
    لو عايز تعرض progress bar لليوزر.
    """
    dest = Path(tempfile.gettempdir()) / "StudyNotes_Update_Setup.exe"

    req = urllib.request.Request(download_url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if on_progress and total:
                    on_progress(int(downloaded * 100 / total))

    return dest


def launch_silent_installer(installer_path: Path) -> None:
    """
    بتشغّل ملف التثبيت بعلامات Inno Setup الصامتة:
    - /VERYSILENT: من غير أي نافذة تثبيت تظهر خالص
    - /SUPPRESSMSGBOXES: من غير أي رسائل تأكيد
    - /NORESTART: متعملش Restart لجهاز اليوزر
    - /CLOSEAPPLICATIONS: يقفل أي نسخة شغالة من StudyNotes لوحده لو لقاها
      (لازم ملف الـ .iss يكون مفعّل فيه CloseApplications)

    البرنامج الحالي هيقفل نفسه فورًا بعد ما يشغّل الأمر ده (المفروض
    الكود اللي بينادي الدالة دي يعمل self.root.destroy() بعدها على طول)
    عشان يسيب ملفاته حرة للتثبيت الجديد يكتب عليها.
    """
    subprocess.Popen(
        [
            str(installer_path),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/CLOSEAPPLICATIONS",
        ],
        close_fds=True,
    )
