import numpy as np
import scipy.io.wavfile as wavfile
import scipy.signal as signal
import os

def classify_audio(wav_path):
    """
    Analyzes audio WAV file and classifies it as:
    - 'vehicle': Possible vehicle exhaust / engine rumble (heuristic)
    - 'ets': Transit bus (heavy diesel engine roar, pneumatic air brakes, sustained pass-by)
    - 'weather': Rain / wind broadband noise
    - 'bbq': BBQ / patio noise (lid drop, scraping, sizzling, tongs)
    - 'impulse': Thunder, clap, or sudden spike
    - 'review': Borderline / needs manual review
    """
    if not os.path.exists(wav_path):
        return "review"
        
    try:
        sample_rate, data = wavfile.read(wav_path)
        return classify_samples(data, sample_rate)
    except Exception as e:
        print(f"Classification error for {wav_path}: {e}")
        return "review"


def classify_samples(data, sample_rate):
    """Apply the same heuristic to memory samples without writing a WAV."""
    try:
        if len(data) == 0:
            return "review"
            
        # Convert stereo to mono if needed
        if len(data.shape) > 1:
            data = np.mean(data, axis=1)
            
        data = data.astype(np.float64)
        max_val = np.max(np.abs(data))
        if max_val > 0:
            data = data / max_val  # Normalize to [-1.0, 1.0]

        total_samples = len(data)

        # 1. Temporal Envelope & Crest Factor
        frame_len = int(sample_rate * 0.05)
        num_frames = total_samples // frame_len
        if num_frames < 4:
            return "review"

        rms_envelope = np.array([
            np.sqrt(np.mean(data[i*frame_len : (i+1)*frame_len]**2))
            for i in range(num_frames)
        ])
        
        peak_rms = np.max(rms_envelope)
        mean_rms = np.mean(rms_envelope)
        crest_factor = peak_rms / (mean_rms + 1e-6)

        # 2. Spectral Frequency Analysis
        freqs, psd = signal.welch(data, sample_rate, nperseg=2048)
        total_energy = np.sum(psd) + 1e-12

        # Energy distribution across bands
        low_band = (freqs >= 50) & (freqs <= 400)      # Vehicle exhaust rumble
        mid_band = (freqs > 400) & (freqs <= 1800)     # Engine / tyre roar
        high_band = (freqs >= 2500) & (freqs <= 12000) # Rain hiss / Wind
        air_brake_band = (freqs >= 3000) & (freqs <= 7000) # Pneumatic air brake discharge

        low_ratio = np.sum(psd[low_band]) / total_energy
        mid_ratio = np.sum(psd[mid_band]) / total_energy
        high_ratio = np.sum(psd[high_band]) / total_energy
        air_ratio = np.sum(psd[air_brake_band]) / total_energy

        # Rain/Wind: Heavy high frequency content (>50%) & weak low frequency rumble (<18%)
        if high_ratio > 0.50 and low_ratio < 0.18:
            return "weather"

        # BBQ / Patio: Close-proximity metallic impact/lid drop/tongs or sizzle (high crest factor, negligible road bass < 12%)
        if (crest_factor > 5.5 and low_ratio < 0.12 and high_ratio > 0.35) or (high_ratio > 0.65 and low_ratio < 0.08):
            return "bbq"

        # Short Impulse / Claps: High crest factor and short duration peak
        if crest_factor > 5.0 and low_ratio < 0.25:
            return "impulse"

        # Vehicle Exhaust / Engine: Low/Mid frequency dominance (>35% energy below 1.8kHz)
        if (low_ratio + mid_ratio) > 0.35 and high_ratio < 0.55:
            # Transit Bus (ETS): Check for sustained diesel rumble envelope or pneumatic air brake discharge
            sustained_ratio = np.sum(rms_envelope > (peak_rms * 0.35)) / num_frames
            is_air_brake = (air_ratio > 0.10 and low_ratio > 0.35)
            is_sustained_bus = (sustained_ratio > 0.32 and crest_factor < 3.2 and low_ratio > 0.50)
            if is_air_brake or is_sustained_bus:
                return "ets"
            return "vehicle"

        return "review"

    except Exception as e:
        print(f"Classification error: {e}")
        return "review"
