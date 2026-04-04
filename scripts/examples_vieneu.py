"""
Simple example demonstrating Vieneu TTS library usage.
Vieneu is a Vietnamese text-to-speech engine.
"""

import numpy as np
import sounddevice as sd
from vieneu import Vieneu

text = 'Vì vậy nó sẽ là "ACMdemo. Stephanthegiáo viên. com. Và điều này là do tôi hiện đang sở'

def example_basic_tts():
    """Basic example: generate and play audio from text."""
    print("=== Example 1: Basic TTS ===")

    # Initialize Vieneu engine
    engine = Vieneu()

    # Text to convert to speech (Vietnamese)
    print(f"Text: {text}")

    # Generate audio
    audio = engine.infer(text=text)

    if audio is not None:
        # Convert to numpy array if needed
        if isinstance(audio, np.ndarray):
            audio_data = audio
        else:
            audio_data = np.array(audio, dtype=np.float32)

        print(f"Audio generated: {len(audio_data)} samples")

        # Play audio
        sample_rate = 24000  # Vieneu default sample rate
        print("Playing audio...")
        sd.play(audio_data, samplerate=sample_rate)
        sd.wait()
        print("Done!")
    else:
        print("Error: Failed to generate audio")


def example_english_text():
    """Example with English text."""
    print("\n=== Example 2: English Text ===")

    engine = Vieneu()

    # English text - Vieneu supports mixed EN/VI
    print(f"Text: {text}")

    audio = engine.infer(text=text)

    if audio is not None:
        audio_data = np.array(audio, dtype=np.float32) if not isinstance(audio, np.ndarray) else audio
        print(f"Audio generated: {len(audio_data)} samples")
        print("Playing audio...")
        sd.play(audio_data, samplerate=24000)
        sd.wait()
        print("Done!")


def example_mixed_language():
    """Example with mixed Vietnamese and English."""
    print("\n=== Example 3: Mixed Language (EN/VI) ===")

    engine = Vieneu()

    # Mixed language text
    print(f"Text: {text}")

    audio = engine.infer(text=text)

    if audio is not None:
        audio_data = np.array(audio, dtype=np.float32) if not isinstance(audio, np.ndarray) else audio
        print(f"Audio generated: {len(audio_data)} samples")
        print("Playing audio...")
        sd.play(audio_data, samplerate=24000)
        sd.wait()
        print("Done!")


def example_with_speed_control():
    """Example with speed control (resample audio)."""
    print("\n=== Example 4: Speed Control ===")

    from scipy import signal

    engine = Vieneu()
    print(f"Text: {text}")

    audio = engine.infer(text=text)

    if audio is not None:
        audio_data = np.array(audio, dtype=np.float32) if not isinstance(audio, np.ndarray) else audio

        # Speed up by 1.5x (resample to fewer samples)
        speed_factor = 1.5
        new_length = int(len(audio_data) / speed_factor)
        audio_fast = signal.resample(audio_data, new_length).astype(np.float32)

        # Clamp to valid range
        audio_fast = np.clip(audio_fast, -1.0, 1.0)

        print(f"Original audio: {len(audio_data)} samples")
        print(f"Speed-up audio: {len(audio_fast)} samples (1.5x faster)")
        print("Playing speed-up audio...")
        sd.play(audio_fast, samplerate=24000)
        sd.wait()
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

    for i, text in enumerate(texts, 1):
        print(f"\nProcessing text {i}: {text}")
        audio = engine.infer(text=text)

        if audio is not None:
            audio_data = np.array(audio, dtype=np.float32) if not isinstance(audio, np.ndarray) else audio
            print(f"  Generated: {len(audio_data)} samples")
            print(f"  Playing...")
            sd.play(audio_data, samplerate=24000)
            sd.wait()
        else:
            print(f"  Error generating audio")


if __name__ == "__main__":
    print("Vieneu TTS Library Examples\n")

    try:
        # Run all examples
        example_basic_tts()
        example_english_text()
        example_mixed_language()
        example_with_speed_control()
        example_batch_inference()

        print("\n=== All examples completed! ===")

    except Exception as e:
        print(f"Error: {e}")
        print("\nMake sure Vieneu and sounddevice are installed:")
        print("  pip install vieneu sounddevice scipy")
