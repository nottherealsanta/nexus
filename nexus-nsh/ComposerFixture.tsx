// Finite mock SSE responses close immediately. Keep the loaded test view live
// to exercise the real composer and HTTP mutation path after that closure.
import "./App";
import { effect } from "nsh";
import { connection, sync } from "./src/store";
effect(() => {
  if (connection().kind === "reconnecting") {
    sync.stop();
    connection.set({ kind: "live" });
  }
});
