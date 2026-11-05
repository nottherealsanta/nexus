import librosa, numpy as np, sys
y, sr = librosa.load(sys.argv[1], sr=22050, mono=True, duration=110)
hop=256
S = np.abs(librosa.stft(y, hop_length=hop))
f = librosa.fft_frequencies(sr=sr); t=librosa.frames_to_time(np.arange(S.shape[1]),sr=sr,hop_length=hop)
full=20*np.log10(S.mean(0)+1e-9); hi=20*np.log10(S[f>4000].mean(0)+1e-9); low=20*np.log10(S[f<120].mean(0)+1e-9)
for a,b in [(13.3,16.2),(45.8,49.2),(78.8,81),(92.4,95.6),(7,8.5)]:
    print(f"--- {a}-{b}")
    for x in np.arange(a,b,0.1):
        m=(t>=x)&(t<x+0.1)
        print(f"{x:.1f}:{full[m].mean():.0f}/{low[m].mean():.0f}/{hi[m].mean():.0f}",end="  ")
    print()
# beat grid fit
_,beats=librosa.beat.beat_track(y=y,sr=sr,units='time')
b=beats[(beats>16)&(beats<79)]
k=np.round((b-b[0])/0.4876)
p=np.polyfit(k,b,1); print("period",p[0],"bpm",60/p[0],"offset",p[1]%p[0], "resid",np.abs(np.polyval(p,k)-b).max())
