r"""
تحويل معادلات LaTeX ($...$ للمعادلة داخل السطر، $$...$$ للمعادلة في سطر
مستقل) لصور PNG (base64) عشان تتعرض جوه tkinterweb.

السبب: tkinterweb بيعرض HTML/CSS بس، ودعمه لجافاسكريبت جزئي جدًا (محتاج
PythonMonkey إضافي)، فمينفعش نشغّل MathJax فيه عشان نعرض LaTeX. الحل هنا:
نرسم المعادلة كصورة بمكتبة matplotlib (mathtext) وقت التوليد، ونحطها
كـ <img> جوه الـ HTML.

ملحوظة: mathtext بتاعة matplotlib بتغطي أغلب صيغ المعادلات الشائعة (كسور،
مؤشرات علوية/سفلية، رموز يونانية، \cdot \times \tanh...إلخ) لكنها مش محرك
LaTeX كامل (مثلاً بيئات \begin{align} مش مدعومة). لو معادلة معينة فشلت
رسمها، بيترك النص الخام زي ما هو بدل ما يوقف البرنامج.
"""

import base64
import io
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

_cache: dict[tuple, str] = {}


def _render_png_b64(latex: str, fontsize: int, color: str) -> str:
    key = (latex, fontsize, color)
    if key in _cache:
        return _cache[key]

    fig = plt.figure()
    try:
        fig.patch.set_alpha(0)
        text_obj = fig.text(0, 0, f"${latex}$", fontsize=fontsize, color=color)
        fig.canvas.draw()
        bbox = text_obj.get_window_extent()
        width = max(bbox.width / fig.dpi + 0.2, 0.3)
        height = max(bbox.height / fig.dpi + 0.15, 0.2)
        plt.close(fig)

        fig = plt.figure(figsize=(width, height))
        fig.patch.set_alpha(0)
        fig.text(0.03, 0.15, f"${latex}$", fontsize=fontsize, color=color)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", transparent=True, dpi=160,
                     bbox_inches="tight", pad_inches=0.04)
        plt.close(fig)
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode("ascii")
        _cache[key] = b64
        return b64
    except Exception:
        plt.close(fig)
        return ""


_BLOCK_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_INLINE_RE = re.compile(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", re.DOTALL)


def render_math_to_html_images(
    text: str, fg_color: str = "#eceef4",
    inline_fontsize: int = 13, block_fontsize: int = 16,
) -> str:
    """يستبدل أي $$...$$ أو $...$ في النص بصورة <img> مرسومة، ويرجع نص
    HTML جاهز يتحط جوه الـ markdown قبل التحويل الكامل. لازم تتنفذ قبل
    markdown.markdown() عشان الـ $ ماتتأثرش بأي escaping."""

    def _block_sub(m: re.Match) -> str:
        latex = m.group(1).strip()
        b64 = _render_png_b64(latex, block_fontsize, fg_color)
        if not b64:
            return m.group(0)
        return (
            '<div style="text-align:center;margin:12px 0;">'
            f'<img src="data:image/png;base64,{b64}" /></div>'
        )

    def _inline_sub(m: re.Match) -> str:
        latex = m.group(1).strip()
        b64 = _render_png_b64(latex, inline_fontsize, fg_color)
        if not b64:
            return m.group(0)
        return (
            f'<img src="data:image/png;base64,{b64}" '
            'style="vertical-align:middle;" />'
        )

    text = _BLOCK_RE.sub(_block_sub, text)
    text = _INLINE_RE.sub(_inline_sub, text)
    return text