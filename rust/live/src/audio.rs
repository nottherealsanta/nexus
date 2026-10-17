//! Audio I/O abstraction. The call pipeline only ever touches [`Pipes`]
//! (mono 48 kHz i16 rings); an [`AudioBackend`] connects them to hardware
//! (cpal, feature `audio`) or to nothing (tests, [`FakeBackend`]).

use crate::dsp::{now_ms, SampleRing, INPUT_RING_SAMPLES, PLAYBACK_SAMPLES};
use std::any::Any;
use std::sync::atomic::{AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

/// Most recent output RMS, written by the decoder, read by the level ticker.
#[derive(Debug, Default)]
pub struct OutLevel {
    bits: AtomicU32,
    at_ms: AtomicU64,
}

impl OutLevel {
    /// A reading older than this counts as silence.
    const STALE_MS: u64 = 60;

    pub fn set(&self, rms: f32) {
        self.bits.store(rms.to_bits(), Ordering::Relaxed);
        self.at_ms.store(now_ms() + 1, Ordering::Relaxed);
    }

    pub fn get(&self) -> f32 {
        let at = self.at_ms.load(Ordering::Relaxed);
        if at == 0 || now_ms() + 1 > at + Self::STALE_MS {
            return 0.0;
        }
        f32::from_bits(self.bits.load(Ordering::Relaxed))
    }
}

/// Shared buffers between the call pipeline and a backend.
#[derive(Clone)]
pub struct Pipes {
    /// Captured microphone audio (backend pushes, pipeline pops).
    pub capture: Arc<Mutex<SampleRing>>,
    /// Decoded assistant audio (pipeline pushes, backend pops).
    pub playback: Arc<Mutex<SampleRing>>,
    pub out_level: Arc<OutLevel>,
}

impl Pipes {
    pub fn new() -> Self {
        Self {
            capture: Arc::new(Mutex::new(SampleRing::new(INPUT_RING_SAMPLES))),
            playback: Arc::new(Mutex::new(SampleRing::new(PLAYBACK_SAMPLES))),
            out_level: Arc::new(OutLevel::default()),
        }
    }
}

impl Default for Pipes {
    fn default() -> Self {
        Self::new()
    }
}

/// Keeps devices alive; dropping it closes the microphone and speaker.
pub type AudioGuard = Box<dyn Any + Send>;

pub trait AudioBackend: Send {
    fn open(&mut self, pipes: &Pipes) -> Result<AudioGuard, String>;
}

/// Opens nothing; tests drive [`Pipes`] directly.
pub struct FakeBackend;

impl AudioBackend for FakeBackend {
    fn open(&mut self, _pipes: &Pipes) -> Result<AudioGuard, String> {
        Ok(Box::new(()))
    }
}

/// Used when the crate is built without the `audio` feature.
pub struct UnavailableBackend;

impl AudioBackend for UnavailableBackend {
    fn open(&mut self, _pipes: &Pipes) -> Result<AudioGuard, String> {
        Err("nexus-live was built without audio support (feature `audio`)".into())
    }
}

#[cfg(feature = "audio")]
pub use cpal_backend::CpalBackend;

#[cfg(feature = "audio")]
mod cpal_backend {
    use super::*;
    use crate::dsp::{f32_to_i16, i16_to_f32, LinearResampler, SAMPLE_RATE};
    use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
    use cpal::{FromSample, SampleFormat, SizedSample};
    use std::collections::VecDeque;
    use std::sync::mpsc;

    pub struct CpalBackend;

    struct Guard {
        stop: Option<mpsc::Sender<()>>,
        thread: Option<std::thread::JoinHandle<()>>,
    }

    impl Drop for Guard {
        fn drop(&mut self) {
            drop(self.stop.take());
            if let Some(t) = self.thread.take() {
                let _ = t.join();
            }
        }
    }

    impl AudioBackend for CpalBackend {
        fn open(&mut self, pipes: &Pipes) -> Result<AudioGuard, String> {
            let pipes = pipes.clone();
            let (stop_tx, stop_rx) = mpsc::channel::<()>();
            let (ready_tx, ready_rx) = mpsc::channel::<Result<(), String>>();
            // cpal streams are not Send on every platform: own them on one thread.
            let thread = std::thread::Builder::new()
                .name("nexus-live-audio".into())
                .spawn(move || match build(&pipes) {
                    Ok(streams) => {
                        let _ = ready_tx.send(Ok(()));
                        let _ = stop_rx.recv(); // returns when the guard drops
                        drop(streams);
                    }
                    Err(e) => {
                        let _ = ready_tx.send(Err(e));
                    }
                })
                .map_err(|e| e.to_string())?;
            match ready_rx.recv() {
                Ok(Ok(())) => Ok(Box::new(Guard { stop: Some(stop_tx), thread: Some(thread) })),
                Ok(Err(e)) => {
                    let _ = thread.join();
                    Err(e)
                }
                Err(_) => Err("audio thread died".into()),
            }
        }
    }

    fn build(pipes: &Pipes) -> Result<(cpal::Stream, cpal::Stream), String> {
        let host = cpal::default_host();
        let input = host.default_input_device().ok_or("no default input device (microphone)")?;
        let output = host.default_output_device().ok_or("no default output device (speaker)")?;
        let in_cfg = input.default_input_config().map_err(|e| format!("input config: {e}"))?;
        let out_cfg = output.default_output_config().map_err(|e| format!("output config: {e}"))?;
        let in_stream = match in_cfg.sample_format() {
            SampleFormat::F32 => build_in::<f32>(&input, &in_cfg, pipes),
            SampleFormat::I16 => build_in::<i16>(&input, &in_cfg, pipes),
            SampleFormat::U16 => build_in::<u16>(&input, &in_cfg, pipes),
            f => Err(format!("unsupported input sample format {f:?}")),
        }?;
        let out_stream = match out_cfg.sample_format() {
            SampleFormat::F32 => build_out::<f32>(&output, &out_cfg, pipes),
            SampleFormat::I16 => build_out::<i16>(&output, &out_cfg, pipes),
            SampleFormat::U16 => build_out::<u16>(&output, &out_cfg, pipes),
            f => Err(format!("unsupported output sample format {f:?}")),
        }?;
        in_stream.play().map_err(|e| format!("start input: {e}"))?;
        out_stream.play().map_err(|e| format!("start output: {e}"))?;
        Ok((in_stream, out_stream))
    }

    fn build_in<T>(dev: &cpal::Device, cfg: &cpal::SupportedStreamConfig, pipes: &Pipes) -> Result<cpal::Stream, String>
    where
        T: SizedSample,
        f32: FromSample<T>,
    {
        let channels = cfg.channels() as usize;
        let mut rs = LinearResampler::new(cfg.sample_rate().0, SAMPLE_RATE);
        let ring = pipes.capture.clone();
        let mut mono: Vec<f32> = Vec::new();
        let mut out: Vec<f32> = Vec::new();
        dev.build_input_stream(
            &cfg.config(),
            move |data: &[T], _| {
                mono.clear();
                out.clear();
                for frame in data.chunks(channels) {
                    let sum: f32 = frame.iter().map(|s| <f32 as FromSample<T>>::from_sample_(*s)).sum();
                    mono.push(sum / channels as f32);
                }
                rs.process(&mono, &mut out);
                let pcm: Vec<i16> = out.iter().map(|s| f32_to_i16(*s)).collect();
                if let Ok(mut r) = ring.lock() {
                    // Keep the newest audio if the pipeline lags: stale mic is worse than a gap.
                    r.push_keep_latest(&pcm);
                }
            },
            |e| eprintln!("nexus-live: input stream error: {e}"),
            None,
        )
        .map_err(|e| format!("open input: {e}"))
    }

    fn build_out<T>(dev: &cpal::Device, cfg: &cpal::SupportedStreamConfig, pipes: &Pipes) -> Result<cpal::Stream, String>
    where
        T: SizedSample + FromSample<f32>,
    {
        let channels = cfg.channels() as usize;
        let out_hz = cfg.sample_rate().0 as usize;
        let mut rs = LinearResampler::new(SAMPLE_RATE, cfg.sample_rate().0);
        let ring = pipes.playback.clone();
        let mut pending: VecDeque<f32> = VecDeque::new();
        let mut scratch: Vec<f32> = Vec::new();
        dev.build_output_stream(
            &cfg.config(),
            move |data: &mut [T], _| {
                let want = data.len() / channels;
                while pending.len() < want {
                    // Pull what this callback still needs (in 48 kHz samples) rather than a
                    // fixed 10 ms, so large device buffers are not padded with silence while
                    // audio waits in the ring. Underruns still play silence, never stall.
                    let need = ((want - pending.len()) * SAMPLE_RATE as usize).div_ceil(out_hz).max(1);
                    let mut chunk: Vec<i16> = ring.lock().map(|mut r| r.pop_upto(need)).unwrap_or_default();
                    chunk.resize(need, 0);
                    let f: Vec<f32> = chunk.iter().map(|s| i16_to_f32(*s)).collect();
                    scratch.clear();
                    rs.process(&f, &mut scratch);
                    pending.extend(scratch.iter());
                }
                for frame in data.chunks_mut(channels) {
                    let v = T::from_sample(pending.pop_front().unwrap_or(0.0));
                    frame.iter_mut().for_each(|s| *s = v);
                }
            },
            |e| eprintln!("nexus-live: output stream error: {e}"),
            None,
        )
        .map_err(|e| format!("open output: {e}"))
    }
}
