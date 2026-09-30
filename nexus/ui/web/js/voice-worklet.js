class NexusVoiceResampler extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.targetRate = 16000;
    this.step = sampleRate / this.targetRate;
    this.nextPosition = 0;
    this.position = 0;
    this.previous = 0;
    this.hasPrevious = false;
    this.block = new Int16Array(2048);
    this.used = 0;
    this.port.onmessage = event => { if (event.data?.type === 'flush') { this.flush(); this.port.postMessage({flushed: true}); } };
  }

  process(inputs) {
    const channels = inputs[0];
    const samples = channels && channels[0];
    if (!samples) return true;
    let squareSum = 0;
    for (let i = 0; i < samples.length; i++, this.position++) {
      const current = samples[i];
      squareSum += current * current;
      while (this.nextPosition <= this.position) {
        let value = current;
        if (this.hasPrevious) {
          const fraction = Math.max(0, Math.min(1, this.nextPosition - (this.position - 1)));
          value = this.previous + (current - this.previous) * fraction;
        }
        this.block[this.used++] = Math.max(-32768, Math.min(32767,
          Math.round(value < 0 ? value * 32768 : value * 32767)));
        this.nextPosition += this.step;
        if (this.used === this.block.length) this.flush();
      }
      this.previous = current;
      this.hasPrevious = true;
    }
    this.port.postMessage({level: Math.sqrt(squareSum / samples.length)});
    return true;
  }

  flush() {
    if (!this.used) return;
    const samples = this.block.slice(0, this.used);
    this.port.postMessage({samples}, [samples.buffer]);
    this.block = new Int16Array(2048);
    this.used = 0;
  }
}

registerProcessor('nexus-voice-resampler', NexusVoiceResampler);
