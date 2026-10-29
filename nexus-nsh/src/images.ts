import { signal } from "nsh";

let nextImage = 0;
export type DraftImage = { id: string; name: string; data_url: string };
export const imageDrafts = signal<Record<string, DraftImage[]>>({});
export const MAX_IMAGE_BYTES = 8 * 1024 * 1024;

export async function readImage(file: File): Promise<DraftImage> {
  if (!/^image\/(png|jpeg|gif|webp)$/.test(file.type)) throw new Error("Use PNG, JPEG, GIF or WebP images");
  if (!file.size || file.size > MAX_IMAGE_BYTES) throw new Error("Images must be at most 8 MiB");
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  return { id: String(++nextImage), name: file.name || "clipboard.png", data_url: `data:${file.type};base64,${btoa(binary)}` };
}
