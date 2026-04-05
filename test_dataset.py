"""
Test script: generate WAV files from dataset-test.txt using the same logic as tts_worker.py.

Modes:
  python test_dataset.py            → one WAV per line  → ./test/001_....wav
  python test_dataset.py --merge    → all lines merged  → ./test/merged.wav
"""

import argparse
import logging
import os
import sys

import numpy as np
import soundfile as sf
from vieneu import Vieneu

try:
    from app.audio_utils import SAMPLE_RATE as _AUDIO_SAMPLE_RATE
    from app.audio_utils import infer_audio, speed_up_audio
except ImportError:
    from audio_utils import SAMPLE_RATE as _AUDIO_SAMPLE_RATE
    from audio_utils import infer_audio, speed_up_audio

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SAMPLE_RATE = _AUDIO_SAMPLE_RATE
SPEED_FACTOR = float(os.getenv("W3_TTS_SPEED_FACTOR", "1.5"))
DATASET_FILE = os.path.join(os.path.dirname(__file__), "data", "dataset-test.txt")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "test")


def _infer_and_convert(engine, text):
    """Thin wrapper around audio_utils.infer_audio for backward compatibility."""
    return infer_audio(engine, text, sample_rate=SAMPLE_RATE)


def _speed_up_audio(audio_data, speed_factor=1.2):
    """Thin wrapper around audio_utils.speed_up_audio for backward compatibility."""
    return speed_up_audio(audio_data, speed_factor=speed_factor, sample_rate=SAMPLE_RATE)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate WAV from dataset-test.txt")
    parser.add_argument("--merge", action="store_true",
                        help="Merge all lines into a single file ./test/merged.wav")
    parser.add_argument("--text", type=str, default=None,
                        help="Test a single text string → ./test/text_test.wav")
    args = parser.parse_args()

    if args.text:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        try:
            engine = Vieneu()
        except Exception as err:
            logger.error(f"TTS initialization error: {err}")
            sys.exit(1)
        logger.info(f"Testing text: {args.text}")
        audio_data = _infer_and_convert(engine, args.text)
        if audio_data is None or len(audio_data) < 100:
            logger.error("No audio generated.")
            sys.exit(1)
        audio_data = _speed_up_audio(audio_data, speed_factor=SPEED_FACTOR)
        out_path = os.path.join(OUTPUT_DIR, "text_test.wav")
        sf.write(out_path, audio_data, SAMPLE_RATE, subtype='FLOAT')
        duration_sec = len(audio_data) / SAMPLE_RATE
        logger.info(f"Saved → {out_path} ({duration_sec:.1f}s)")
        sys.exit(0)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    with open(DATASET_FILE, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    mode = "merge → merged.wav" if args.merge else "split → one file per line"
    logger.info(f"Loaded {len(lines)} lines from {DATASET_FILE}")
    logger.info(f"Speed factor: {SPEED_FACTOR}x  |  Mode: {mode}  |  Output dir: {OUTPUT_DIR}")

    try:
        engine = Vieneu()
        logger.info("TTS backend: VieNeu-TTS")
    except Exception as err:
        logger.error(f"TTS initialization error: {err}")
        sys.exit(1)

    ok = 0
    fail = 0
    merge_buffer = []        # (idx, text, audio_data) — used only in --merge mode

    for idx, text in enumerate(lines, start=1):
        logger.info(f"[{idx:03d}/{len(lines)}] {text[:60]}")

        audio_data = _infer_and_convert(engine, text)
        if audio_data is None or len(audio_data) < 100:
            logger.warning(f"  → Skipped (no audio)")
            fail += 1
            continue

        audio_data = _speed_up_audio(audio_data, speed_factor=SPEED_FACTOR)

        if not np.all(np.isfinite(audio_data)):
            logger.warning(f"  → Skipped (non-finite audio)")
            fail += 1
            continue

        duration_sec = len(audio_data) / SAMPLE_RATE

        if args.merge:
            merge_buffer.append((idx, text, audio_data))
            logger.info(f"  → buffered ({duration_sec:.1f}s)")
        else:
            preview = text[:30].replace("/", "-").replace("\\", "-").replace(" ", "_")
            out_path = os.path.join(OUTPUT_DIR, f"{idx:03d}_{preview}.wav")
            sf.write(out_path, audio_data, SAMPLE_RATE, subtype='FLOAT')
            logger.info(f"  → {os.path.basename(out_path)} ({duration_sec:.1f}s)")

        ok += 1

    if args.merge:
        if merge_buffer:
            combined = np.concatenate([a for _, _, a in merge_buffer], axis=0)
            out_path = os.path.join(OUTPUT_DIR, "merged.wav")
            sf.write(out_path, combined, SAMPLE_RATE, subtype='FLOAT')
            total_sec = len(combined) / SAMPLE_RATE
            logger.info(f"\nMerged {ok} chunks → {out_path} ({total_sec:.1f}s total)")

            # Write timestamp index
            index_path = os.path.join(OUTPUT_DIR, "merged_index.txt")
            with open(index_path, "w", encoding="utf-8") as f:
                f.write(f"{'LINE':>4}  {'START':>8}  {'END':>8}  TEXT\n")
                f.write("-" * 80 + "\n")
                cursor = 0.0
                for idx, text, chunk in merge_buffer:
                    end = cursor + len(chunk) / SAMPLE_RATE
                    f.write(f"{idx:>4}  {cursor:>8.2f}  {end:>8.2f}  {text}\n")
                    cursor = end
            logger.info(f"Timestamp index → {index_path}")
        else:
            logger.warning("No audio to merge.")

    logger.info(f"\nDone: {ok} ok, {fail} failed. Files saved to: {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
