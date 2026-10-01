import struct

import numpy as np


def write_calibrated_wav(path, pcm16_bytes, channels, rate, calibration_offset, reference_db=100.0):
    """
    Write 16-bit PCM as a 32-bit float WAV scaled to a fixed calibration:
    0 dBFS (A-weighted RMS) = reference_db dB(A). One gain for every file, so
    relative loudness between events is preserved; float keeps loud events
    from clipping in the file. The calibration is recorded in a LIST/INFO
    comment.
    """
    gain_db = calibration_offset - reference_db
    samples = np.frombuffer(pcm16_bytes, dtype="<i2").astype(np.float32) / 32768.0
    samples *= np.float32(10 ** (gain_db / 20))
    data = samples.astype("<f4").tobytes()

    fmt = struct.pack("<HHIIHH", 3, channels, rate, rate * channels * 4, channels * 4, 32)  # IEEE float
    fact = struct.pack("<I", len(samples) // channels)
    comment = (f"Calibrated: 0 dBFS = {reference_db:.1f} dB(A) (A-weighted RMS). "
               f"Source calibration offset {calibration_offset:.1f} dB, gain {gain_db:+.1f} dB.").encode() + b"\0"
    if len(comment) % 2:
        comment += b"\0"
    info = b"INFO" + b"ICMT" + struct.pack("<I", len(comment)) + comment

    chunks = (b"fmt " + struct.pack("<I", len(fmt)) + fmt
              + b"fact" + struct.pack("<I", len(fact)) + fact
              + b"LIST" + struct.pack("<I", len(info)) + info
              + b"data" + struct.pack("<I", len(data)) + data)
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks)
