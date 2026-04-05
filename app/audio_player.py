"""Audio playback — play float32 numpy arrays via subprocess. Blocks until done."""

import logging
import os
import platform
import subprocess
import tempfile

import soundfile as sf

logger = logging.getLogger(__name__)

_SYSTEM = platform.system()


def play(audio_data, sample_rate: int) -> None:
    """Play a float32 numpy array. Blocks until playback finishes."""
    duration_sec = len(audio_data) / sample_rate
    logger.info(f"[AUDIO  ] {duration_sec:.1f}s | method={'stdin_pipe' if _SYSTEM == 'Linux' else 'temp_file'}")

    if _SYSTEM == "Linux":
        _play_stdin_pipe(audio_data, sample_rate)
    elif _SYSTEM == "Darwin":
        _play_temp_file(audio_data, sample_rate, ["afplay"])
    else:
        logger.warning(f"Unsupported OS for playback: {_SYSTEM}")


def cleanup_old_temp_files() -> None:
    """Remove stale temp WAV files older than 2 hours."""
    temp_dir = os.getenv("W3_TEMP_AUDIO_DIR", tempfile.gettempdir())
    max_age_sec = 7200
    import time
    now = time.time()
    try:
        for fname in os.listdir(temp_dir):
            if not fname.startswith("tmp") or not fname.endswith(".wav"):
                continue
            fpath = os.path.join(temp_dir, fname)
            try:
                if now - os.path.getmtime(fpath) > max_age_sec:
                    os.unlink(fpath)
                    logger.debug(f"Cleaned up old temp file: {fname}")
            except Exception:
                pass
    except Exception as err:
        logger.debug(f"Cleanup error: {err}")


def _play_stdin_pipe(audio_data, sample_rate: int) -> None:
    """Pipe raw PCM directly to paplay stdin — no temp file, lower latency."""
    try:
        proc = subprocess.Popen(
            ["paplay", "--raw", "--format=float32le", f"--rate={sample_rate}", "--channels=1"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.stdin.write(audio_data.tobytes())
        proc.stdin.close()
        proc.wait()
    except FileNotFoundError:
        logger.warning("[AUDIO  ] paplay not found, falling back to temp file")
        _play_temp_file(audio_data, sample_rate, ["paplay", "--latency=100ms"])
    except Exception as err:
        logger.error(f"stdin pipe playback error: {err}")


def _play_temp_file(audio_data, sample_rate: int, player_cmd: list) -> None:
    """Play audio via temp WAV file + subprocess (fallback / macOS)."""
    keep_temp = os.getenv("W3_KEEP_TEMP_AUDIO", "").lower() == "true"
    temp_dir = os.getenv("W3_TEMP_AUDIO_DIR", tempfile.gettempdir())
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", dir=temp_dir, delete=False) as tmp:
            temp_path = tmp.name
        sf.write(temp_path, audio_data, sample_rate, subtype="FLOAT")
        try:
            subprocess.Popen(
                player_cmd + [temp_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).wait()
        except FileNotFoundError:
            logger.error(f"Audio player not found: {player_cmd[0]}")
    except Exception as err:
        logger.error(f"temp file playback error: {err}")
    finally:
        if temp_path and not keep_temp:
            try:
                os.unlink(temp_path)
            except Exception:
                pass
