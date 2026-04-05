"""
Simple example demonstrating Vieneu TTS library usage.
Vieneu is a Vietnamese text-to-speech engine.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import numpy as np
from vieneu import Vieneu
import audio_player

SAMPLE_RATE = 24000
text = 'Vì vậy nó sẽ là "ACMdemo. Stephanthegiáo viên. com. Và điều này là do tôi hiện đang sở'


def _to_array(audio):
    if isinstance(audio, np.ndarray):
        return audio.astype(np.float32)
    return np.array(audio, dtype=np.float32)


def example_basic_tts():
    """Basic example: generate and play audio from text."""
    print("=== Example 1: Basic TTS ===")
    engine = Vieneu()
    print(f"Text: {text}")
    audio = engine.infer(text=text)
    if audio is not None:
        audio_data = _to_array(audio)
        print(f"Audio generated: {len(audio_data)} samples")
        print("Playing audio...")
        audio_player.play(audio_data, SAMPLE_RATE)
        print("Done!")
    else:
        print("Error: Failed to generate audio")


def example_english_text():
    """Example with English text."""
    print("\n=== Example 2: English Text ===")
    engine = Vieneu()
    print(f"Text: {text}")
    audio = engine.infer(text=text)
    if audio is not None:
        audio_data = _to_array(audio)
        print(f"Audio generated: {len(audio_data)} samples")
        print("Playing audio...")
        audio_player.play(audio_data, SAMPLE_RATE)
        print("Done!")


def example_mixed_language():
    """Example with mixed Vietnamese and English."""
    print("\n=== Example 3: Mixed Language (EN/VI) ===")
    engine = Vieneu()
    print(f"Text: {text}")
    audio = engine.infer(text=text)
    if audio is not None:
        audio_data = _to_array(audio)
        print(f"Audio generated: {len(audio_data)} samples")
        print("Playing audio...")
        audio_player.play(audio_data, SAMPLE_RATE)
        print("Done!")


def example_with_speed_control():
    """Example with speed control (resample audio)."""
    print("\n=== Example 4: Speed Control ===")
    from scipy import signal
    engine = Vieneu()
    print(f"Text: {text}")
    audio = engine.infer(text=text)
    if audio is not None:
        audio_data = _to_array(audio)
        speed_factor = 1.5
        new_length = int(len(audio_data) / speed_factor)
        audio_fast = signal.resample(audio_data, new_length).astype(np.float32)
        audio_fast = np.clip(audio_fast, -1.0, 1.0)
        print(f"Original audio: {len(audio_data)} samples")
        print(f"Speed-up audio: {len(audio_fast)} samples (1.5x faster)")
        print("Playing speed-up audio...")
        audio_player.play(audio_fast, SAMPLE_RATE)
        print("Done!")


def example_batch_inference():
    """Example processing multiple texts."""
    print("\n=== Example 5: Batch Inference ===")
    engine = Vieneu()
    texts = [
        "Ví dụ thứ nhất",
        "Example number two",
        "Và ví dụ thứ ba"
    ]
    for i, t in enumerate(texts, 1):
        print(f"\nProcessing text {i}: {t}")
        audio = engine.infer(text=t)
        if audio is not None:
            audio_data = _to_array(audio)
            print(f"  Generated: {len(audio_data)} samples")
            print(f"  Playing...")
            audio_player.play(audio_data, SAMPLE_RATE)
        else:
            print(f"  Error generating audio")


if __name__ == "__main__":
    print("Vieneu TTS Library Examples\n")
    try:
        example_basic_tts()
        example_english_text()
        example_mixed_language()
        example_with_speed_control()
        example_batch_inference()
        print("\n=== All examples completed! ===")
    except Exception as e:
        print(f"Error: {e}")
        print("\nMake sure Vieneu and scipy are installed:")
        print("  pip install vieneu scipy")
