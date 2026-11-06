use rodio::{DeviceSinkBuilder, DeviceSinkError, MixerDeviceSink};

pub(crate) fn open() -> Result<MixerDeviceSink, DeviceSinkError> {
    // Rodio forces a fixed buffer even for the device's default format. CoreAudio
    // devices can reject that size; let the OS choose it, and keep any format
    // fallback on the selected device rather than routing speech elsewhere.
    let mut sink = DeviceSinkBuilder::from_default_device()?
        .with_buffer_size(cpal::BufferSize::Default)
        .open_sink_or_fallback()?;
    sink.log_on_drop(false);
    Ok(sink)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[ignore = "opens the real default audio output device"]
    fn system_buffer_opens_on_the_default_device() {
        match DeviceSinkBuilder::open_default_sink() {
            Ok(mut sink) => {
                sink.log_on_drop(false);
                eprintln!("Rodio default: {:?}", sink.config());
            }
            Err(error) => eprintln!("Rodio default failed: {error:?}"),
        }
        let sink = open().expect("system-buffer output should open");
        assert_eq!(*sink.config().buffer_size(), cpal::BufferSize::Default);
        eprintln!("System buffer: {:?}", sink.config());
    }
}
