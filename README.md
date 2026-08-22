<div align="center">

<img src="logo.svg" alt="StudyNotes Logo" width="120" />

# StudyNotes — Automatic Lecture Recording, Transcription & Summarization

**A desktop app with a GUI that records any lecture or meeting's audio, transcribes it to text, and turns it into organized Markdown notes with rendered math equations — all with zero manual work.**

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Tkinter](https://img.shields.io/badge/GUI-Tkinter-4c4ddc?logo=python&logoColor=white)](https://docs.python.org/3/library/tkinter.html)
[![Gemini](https://img.shields.io/badge/AI-Gemini-8E75B2?logo=googlegemini&logoColor=white)](https://ai.google.dev/)
[![Groq](https://img.shields.io/badge/AI-Groq-F55036?logo=groq&logoColor=white)](https://groq.com/)
[![Whisper](https://img.shields.io/badge/STT-Whisper--large--v3-1f8a4c)](https://github.com/openai/whisper)

</div>

---

## What does this project do?

- Records your system audio while a lecture/meeting is playing — automatically split into chunks every 30 minutes, so it stays safe whether the session is short or hours long.
- Every recorded chunk is automatically compressed to **Opus** (if `ffmpeg` is installed) to save space, without interrupting the ongoing recording.
- When you're done, one click transcribes the audio to text (Groq Whisper first for speed, Gemini as a fallback), then turns that text into focused notes written in a lecturer's style (Gemini first, Groq as a fallback).
- Notes are saved as a cumulative Markdown file per lecture, and rendered right inside the app — including LaTeX equations (`$...$` and `$$...$$`) converted to images via matplotlib.
- Everything is tracked with a status (recorded / transcribed / transcribed & explained) so you can always pick up where you left off, or delete a specific piece without breaking the rest.

---

## Features

| Feature | Detail |
|---|---|
| 🎙️ **System audio recording** | No external microphone needed, automatically split every 30 minutes |
| 🗜️ **Automatic audio compression** | Background conversion to Opus after each chunk, with zero recording downtime |
| 📝 **Dual-provider transcription** | Groq (`whisper-large-v3`) as the default, Gemini as a fallback on failure |
| 🧠 **AI-powered summarization** | Turns raw transcripts into organized, lecturer-style notes, processed in chunks for long texts |
| ➗ **Math equation rendering** | `$...$` and `$$...$$` rendered as PNG images inside the app via matplotlib mathtext |
| 🗂️ **Per-lecture state tracking** | Tracks which audio chunks are transcribed/explained, with the ability to undo the last notes update |
| 🧹 **Selective deletion** | Delete audio/transcript/notes for a whole lecture, or a single file, without affecting the rest |
| 🖥️ **Bilingual GUI (Arabic/English)** | Built with Tkinter, with correct rendering of Arabic and mixed-language text |
| 📦 **Packageable as .exe** | Ready to build with PyInstaller (`StudyNotes.spec`) |

---

## Project Structure

```
.
├── gui_app.py             # Main GUI application (Tkinter)
├── record_session.py      # Command-line system audio recorder (no GUI)
├── process_lecture.py     # Transcription + summarization (Groq / Gemini)
├── state_manager.py       # Folder setup and shared state across scripts
├── math_render.py         # Converts LaTeX to PNG images rendered in the GUI
├── StudyNotes.spec        # PyInstaller build config for a .exe build
├── requirements.txt       # All required packages
├── .env.example            # Environment variable template
└── (created automatically at runtime)
    ├── .state/             # Per-lecture state (JSON)
    ├── Sound_Recorded/     # Audio files (Opus/FLAC)
    ├── Transcript/         # Raw transcripts (txt)
    └── Markdown/           # Final notes (md)
```

> All of the folders above are created automatically the first time you run any script — no need to create them manually.

---

## Requirements

- Python 3.11+
- `ffmpeg` (optional but recommended) for compressing audio to Opus — if missing, files stay as FLAC (larger size)
- At least one API key from:
  - [Google Gemini](https://ai.google.dev/) → `GEMINI_API_KEY`
  - [Groq](https://console.groq.com/) → `GROQ_API_KEY`

---

## Installation

### 1. Clone the repository

```bash
git clone <your-repo-url>
cd <your-repo-folder>
```

### 2. Create and activate a virtual environment

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```env
GEMINI_API_KEY="your-gemini-api-key"
GROQ_API_KEY="your-groq-api-key"

# Optional: set this if you want data folders stored somewhere other than
# next to the project itself
STUDYNOTES_DIR="D:\Agoor"
```

> If `STUDYNOTES_DIR` isn't set, the folders are created automatically next to the scripts themselves.

---

## Running the App

### GUI (recommended)

```bash
python gui_app.py
```

From the GUI you can: pick or create a lecture, start/stop recording, run transcription and summarization, track the status of each audio chunk, and view notes with rendered equations directly.

### Command-line recording (no GUI)

```bash
python record_session.py
```

### Manually process a specific lecture

```bash
python process_lecture.py "lecture name"
```

---

## Building a .exe (optional)

```bash
pyinstaller StudyNotes.spec
```

The resulting executable will be in the `dist/` folder.

---

## Security Notes

- `.env` is excluded from version control — already covered by `.gitignore`.
- Data folders (`.state`, `Sound_Recorded`, `Transcript`, `Markdown`) are also excluded in `.gitignore` so personal lecture content never gets committed by accident.
- Transcription and summarization send audio/text content to third-party providers (Gemini / Groq) — don't use this project with confidential content unless you've reviewed their privacy policies.

---

<div align="center">
<sub>StudyNotes · built with Python, Tkinter, Gemini &amp; Groq</sub>
</div>
