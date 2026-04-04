# w3-speak-shot

Desktop app (PyQt5) that:
- captures a selected screen region in real time,
- runs Vietnamese OCR with `tesseract`,
- speaks detected text with VieNeu-TTS (Turbo model — bilingual EN/VI, runs on CPU).

## System Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- Tesseract + Vietnamese language data

```sh
# macOS
brew install tesseract tesseract-lang

# Linux (Ubuntu/Debian)
sudo apt-get install tesseract-ocr tesseract-ocr-vie

# Windows
# Download from https://github.com/UB-Mannheim/tesseract/wiki
```

## Installation

```sh
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone the repo
git clone <repo-url>
cd w3-speak-shot

# Create virtual environment and install dependencies
uv venv
uv pip install -r requirements.txt
```

## Run

```sh
# Activate venv then run
source .venv/bin/activate
python app

# Or run directly with uv (no manual activation needed)
uv run python app
```

## Quick Usage

- Move the frame using the **top-left square** handle.
- Resize using the **top-right square** handle.
- **Bottom-left green square** shows capture/running status.
- **Bottom-right `X`** square closes the app.
- Use the `Options` menu:
  - `Start` — begin screen capture and OCR
  - `Stop` — stop capture
  - `Speed` — adjust TTS playback speed (0.75x – 2x)
  - `Auto Speed` — when enabled, automatically increases speed by +0.25x when the TTS queue has 2 or more pending items (default: off)
  - `Close App`

The app persists last window position/size in `app/store/.overlay_state.json`.

## Environment Variables

| Variable              | Default | Description                                                     |
| --------------------- | ------- | --------------------------------------------------------------- |
| `W3_TTS_SPEED_FACTOR` | `1.5`   | Base audio speed-up factor (e.g. `1.0` = normal, `2.0` = 2x faster) |

```sh
W3_TTS_SPEED_FACTOR=1.2 uv run python app
```

## How It Works

```mermaid
flowchart TD
    A[Overlay Frame] --> B[Capture Area with mss]
    B --> C[Mask Overlay UI Artifacts]
    C --> D[Frame Signature Check]
    D -- No Change --> B
    D -- Changed --> E[OCR with Tesseract - vie]
    E --> F[OCRTextProcessor Sanitize Text]
    F --> G{Meaningful New Text?}
    G -- No --> B
    G -- Yes --> H[TTSWorker prepare_text]
    H --> I{Skip?}
    I -- contains œ --> B
    I -- No --> J[Infer Audio with VieNeu Turbo<br/>Cache Result LRU]
    J --> K{Audio too long?}
    K -- Yes, likely stuck --> B
    K -- No --> L[Queue for Playback]
    L --> M[Play Audio via temp WAV file]
    M --> B
```

## Skip Conditions

The TTS worker silently skips a segment and logs `[SKIP   ]` when:

- The text contains `œ` — indicates garbled/corrupt OCR output.
- The generated audio duration exceeds `max(5.0, len(text) * 0.15)` seconds — indicates the model entered a stuck/looping state.

## Source Structure

```
app/
├── __main__.py       Entry point, PyQt5 app + native menu setup
├── overlay.py        Overlay UI, capture loop, frame change detection, OCR orchestration
├── overlay_text.py   OCR text processor — sanitization, noise filtering, similarity comparison
├── tts_worker.py     TTS worker thread — VieNeu inference, LRU audio cache, playback, speed control
└── store/
    ├── logo.icns             App icon (macOS)
    ├── logo.ico              App icon (Windows)
    └── .overlay_state.json   Persisted window position/size
```

## TTS Engine

Uses **VieNeu-TTS Turbo** (`TurboVieNeuTTS`) — GGUF/ONNX format, runs on CPU with a LLaMA backbone.

- Sample rate: 24 000 Hz
- Max context: 4 096 tokens
- Supports bilingual text (English + Vietnamese) without pre-segmentation

## Testing

Generate WAV files from `data/dataset-test.txt` to verify TTS output:

```sh
# Test a single sentence
W3_TTS_SPEED_FACTOR=1.5 uv run test_dataset.py --text "your sentence here"

# Generate one WAV per line → test/001_....wav
W3_TTS_SPEED_FACTOR=1.5 uv run test_dataset.py

# Merge all lines into a single file → test/merged.wav + test/merged_index.txt
W3_TTS_SPEED_FACTOR=1.5 uv run test_dataset.py --merge
```

`merged_index.txt` contains per-segment timestamps for easy audio inspection.

## Troubleshooting

**`TTS initialization error`**
```sh
uv pip install vieneu
```

**No audio output (Linux)**
```sh
# Ensure paplay is available (used for playback)
sudo apt-get install pulseaudio-utils
```

**OCR returns noisy text**
- Verify capture region quality and on-screen font clarity.
- Ensure `tesseract-lang` (Vietnamese data) is installed.
