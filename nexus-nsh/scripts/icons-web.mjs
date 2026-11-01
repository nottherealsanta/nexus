// `nsh package` makes its favicon by downscaling the app's one icon, so the
// pixel-tuned 32 px file in interface/brand never reaches the tab. Replace the
// generated icon after the build, keeping the hashed name its HTML points at.
import { copyFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const out = process.argv[2] ?? "dist";
const icons = join(root, out, "Nexus", "icons");
const generated = readdirSync(icons).filter((name) => /^32\..*\.png$/.test(name));
if (generated.length !== 1) {
  console.error(`icons: expected one 32 px icon in ${icons}, found ${generated.length}`);
  process.exit(1);
}
copyFileSync(join(root, "..", "interface", "brand", "nexus-32.png"), join(icons, generated[0]));
console.log(`icons: replaced ${generated[0]} with interface/brand/nexus-32.png`);
