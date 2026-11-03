# nexus-annotate

Marks drawn on a composer image, shared by the TUI and NSH so both produce the
same pixels. Points are normalised to the image and the pen width is a fraction
of its longer side, so a stroke looks the same at any preview size.

- `flatten` merges strokes into a data URL. This is the image the model and the
  timeline receive; JPEG stays JPEG, everything else becomes PNG, and a PNG over
  8 MiB falls back to JPEG.
- `tile` paints part of a stroke at display size. The TUI sends these to Ghostty
  while the pointer moves, because resending the whole image is too slow. The
  pieces join to the same path that `flatten` draws.
