import librosa, numpy as np, sys
y, sr = librosa.load(sys.argv[1], sr=22050, mono=True, duration=100)
tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units='time')
print("tempo", tempo)
print("beats", np.round(beats,2).tolist())
hop=512
rms = librosa.feature.rms(y=y, hop_length=hop)[0]
t = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)
# per 0.25s loudness dB
for i in np.arange(0, 100, 0.5):
    m=(t>=i)&(t<i+0.5)
    print(f"{i:5.1f} {20*np.log10(rms[m].mean()+1e-9):6.1f}", end=" | " if int(i*2)%6!=5 else "\n")
on = librosa.onset.onset_detect(y=y, sr=sr, units='time', backtrack=False)
env = librosa.onset.onset_strength(y=y, sr=sr)
et = librosa.times_like(env, sr=sr)
# strong onsets
strong = [ (round(float(x),2)) for x in on if env[np.argmin(abs(et-x))] > np.percentile(env,97)]
print("\nstrong onsets", strong)
# low-band energy (kick/bass) per 0.5s
S = np.abs(librosa.stft(y, hop_length=hop))
f = librosa.fft_frequencies(sr=sr)
low = S[f<150].mean(0); hi = S[f>4000].mean(0)
print("low/hi per 1s:")
for i in range(0,100):
    m=(t>=i)&(t<i+1)
    print(f"{i}:{20*np.log10(low[m].mean()+1e-9):.0f}/{20*np.log10(hi[m].mean()+1e-9):.0f}", end="  ")
