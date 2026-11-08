#!/usr/bin/env node
// Make a packaged macOS/iOS NSH app able to reach Nexus Core over plain HTTP on
// the network (the iOS and macOS App Transport Security defaults block it), then
// re-sign it ad hoc.
//
//   node scripts/native-info.mjs [<Name>.app] [--launch-ios] [--launch-macos]
//
// A device .ipa is signed by `nsh package` before this can run: package without
// --sign, run this, then sign the .app with your identity and profile.
import { execFileSync, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const args = process.argv.slice(2);
const app = path.resolve(args.find((a) => !a.startsWith("--")) ?? ".nsh/out/Nexus.app");
// A macOS bundle keeps it in Contents; an iOS bundle at its root.
const plist = [path.join(app, "Contents", "Info.plist"), path.join(app, "Info.plist")].find((p) => fs.existsSync(p));
if (!plist) {
  console.error(`native-info: no Info.plist in ${app} (package the app first)`);
  process.exit(2);
}

const plistBuddy = (...command) =>
  execFileSync("/usr/libexec/PlistBuddy", ["-c", command.join(" "), plist], { stdio: ["ignore", "pipe", "pipe"] });

/** Add (or replace) a key; PlistBuddy has no upsert. */
function set(command, key) {
  try { plistBuddy("Delete", `:${key}`); } catch {}
  plistBuddy(...command);
}

try {
  set(["Add", ":NSAppTransportSecurity", "dict"], "NSAppTransportSecurity");
  // Core is reached by a bare address (often a private/tailnet IP), and ATS
  // treats some of those as public; allow plain HTTP for this direct connection.
  // Arbitrary loads alone (no NSAllowsLocalNetworking, which overrides it for
  // connections iOS classifies as local) covers every address the app may use.
  plistBuddy("Add", ":NSAppTransportSecurity:NSAllowsArbitraryLoads", "bool", "true");
  const device = plistBuddy("Print", ":CFBundleSupportedPlatforms:0").toString().includes("iPhone");
  if (device) {
    set(["Add", ":NSLocalNetworkUsageDescription", "string",
      "Nexus connects to Nexus Core on your local network."], "NSLocalNetworkUsageDescription");
  }
  set(["Add", ":NSMicrophoneUsageDescription", "string",
    "Nexus records your voice for dictation and Manager voice, and sends it to Nexus Core for speech recognition."], "NSMicrophoneUsageDescription");
} catch (error) {
  console.error(`native-info: cannot edit ${plist}: ${error.message}`);
  process.exit(1);
}

// The edit breaks the package's signature; an ad-hoc one runs locally (a device
// build still needs a real identity afterwards).
const signed = spawnSync("codesign", ["--force", "--deep", "--sign", "-", app], { stdio: "ignore" });
if (signed.status !== 0) console.error("native-info: ad-hoc signing failed; sign the app yourself before installing it");

const bundle = plistBuddy("Print", ":CFBundleIdentifier").toString().trim();
console.error(`native-info: ${path.basename(app)} allows local-network HTTP (${bundle})`);

const simctl = (...argv) => execFileSync("xcrun", ["simctl", ...argv], { stdio: ["ignore", "pipe", "pipe"] });
if (args.includes("--launch-ios")) {
  let booted = simctl("list", "devices", "booted").toString().split("\n").filter((line) => line.includes("Booted"));
  if (!booted.length) {
    const available = simctl("list", "devices", "available").toString().split("\n")
      .filter((line) => /^\s{4}\S/.test(line) && !line.includes("(unavailable)"));
    const device = available.find((line) => line.includes("iPhone")) ?? available[0];
    if (!device) {
      console.error("native-info: no iOS simulator is installed; run `xcodebuild -downloadPlatform iOS`");
      process.exit(1);
    }
    simctl("boot", device.split("(")[1].split(")")[0]);
    execFileSync("open", ["-a", "Simulator"], { stdio: "ignore" });
    for (let i = 0; i < 60; i++) {
      if (simctl("list", "devices", "booted").toString().includes("Booted")) break;
      execFileSync("sleep", ["1"]);
    }
  }
  simctl("install", "booted", app);
  simctl("launch", "booted", bundle);
  console.error(`native-info: launched ${bundle} in the simulator`);
}
if (args.includes("--launch-macos")) execFileSync("open", [app], { stdio: "ignore" });
