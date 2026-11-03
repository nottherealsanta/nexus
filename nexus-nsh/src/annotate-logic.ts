// Marks on a composer image: the same geometry as `nexus-annotate`, which
// flattens them on Done, so what the canvas shows is what the model receives.

export const PEN_WIDTH = 0.004;
const MIN_PEN_PIXELS = 2;

export type Point = { x: number; y: number };
/** Points run 0 to 1 across the image, so a stroke survives a resize. */
export type Stroke = { color: string; points: Point[] };
export type Rect = { x: number; y: number; width: number; height: number };

export function penWidth(width: number, height: number): number {
  return Math.max(PEN_WIDTH * Math.max(width, height), MIN_PEN_PIXELS);
}

/** The picture scaled to fit `room` and centred in it; small pictures grow. */
export function fit(natural: { width: number; height: number }, room: { width: number; height: number }): Rect {
  const width = Math.max(natural.width, 1);
  const height = Math.max(natural.height, 1);
  const scale = Math.min(room.width / width, room.height / height);
  const fitted = { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)) };
  return { x: Math.floor((room.width - fitted.width) / 2), y: Math.floor((room.height - fitted.height) / 2), ...fitted };
}

export function normalise(x: number, y: number, size: { width: number; height: number }): Point {
  const clamp = (value: number) => Math.min(1, Math.max(0, value));
  return { x: clamp(x / Math.max(size.width, 1)), y: clamp(y / Math.max(size.height, 1)) };
}

export function extend(stroke: Stroke, point: Point): Stroke {
  const last = stroke.points[stroke.points.length - 1];
  return last && last.x === point.x && last.y === point.y ? stroke : { ...stroke, points: [...stroke.points, point] };
}

type Pen = {
  beginPath(): void;
  moveTo(x: number, y: number): void;
  lineTo(x: number, y: number): void;
  quadraticCurveTo(cx: number, cy: number, x: number, y: number): void;
  arc(x: number, y: number, radius: number, start: number, end: number): void;
  stroke(): void;
  fill(): void;
  strokeStyle: unknown;
  fillStyle: unknown;
  lineWidth: unknown;
  lineCap: unknown;
  lineJoin: unknown;
};

/** Midpoint quadratics ending in a straight tail, as in `nexus-annotate`; a tap is a dot. */
export function drawStroke(cx: Pen, stroke: Stroke, width: number, height: number) {
  const pen = penWidth(width, height);
  const at = (i: number) => ({ x: stroke.points[i].x * width, y: stroke.points[i].y * height });
  const last = stroke.points.length - 1;
  if (last < 0) return;
  cx.beginPath();
  if (last === 0) {
    const { x, y } = at(0);
    cx.fillStyle = stroke.color;
    cx.arc(x, y, pen / 2, 0, Math.PI * 2);
    cx.fill();
    return;
  }
  cx.strokeStyle = stroke.color;
  cx.lineWidth = pen;
  cx.lineCap = "round";
  cx.lineJoin = "round";
  const start = at(0);
  cx.moveTo(start.x, start.y);
  for (let i = 1; i < last; i++) {
    const control = at(i);
    const next = at(i + 1);
    cx.quadraticCurveTo(control.x, control.y, (control.x + next.x) / 2, (control.y + next.y) / 2);
  }
  const end = at(last);
  cx.lineTo(end.x, end.y);
  cx.stroke();
}

/** The limit paste checks, in base64 characters, with this image's new size. */
export function withinMessageLimit(sizes: number[]): boolean {
  return sizes.reduce((sum, size) => sum + size, 0) <= (8 * 1024 * 1024 * 4) / 3 + 256;
}
