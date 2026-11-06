import { describe, expect, test } from "nsh/test";
import { setApiBase } from "./src/core/transport";
import { closeTerminalPanel, controlTerminal, inputTerminal, refreshTerminals, requestTerminalClose, resizeTerminal, selectTerminal, setTerminalSender, terminalControlled, terminalError, terminalItems, terminalNotice, terminalSelected } from "./src/terminal-panel";
import { confirmDialog, currentProject } from "./src/store";

const resource = {id:"t",name:"Shell",project_id:"p",workspace:"/work",session_id:null,creator:"test",status:"running" as const,created:1,ended:null,rows:24,cols:80,exit_code:null,signal:null,controller:null};
const reply = (body: unknown) => new Response(JSON.stringify(body), {status:200});
const tick = () => new Promise(r => setTimeout(r, 25));

describe("terminal controller", () => {
  test("closing a running resource requires the shared confirmation", () => {
    terminalItems.set([resource]); terminalSelected.set("t"); confirmDialog.set(null);
    requestTerminalClose();
    expect(confirmDialog()?.title).toBe("Close running terminal?");
    expect(confirmDialog()?.confirm).toBe("Close terminal");
    expect(confirmDialog()?.destructive).toBe(true);
    confirmDialog.set(null); terminalSelected.set(null);
  });
  test("confirmation terminates the originally selected resource", async () => {
    setApiBase("http://nexus.test"); currentProject.set("p");
    terminalItems.set([resource, {...resource, id:"other"}]); terminalSelected.set("t");
    const original = globalThis.fetch, calls: {url:string; method:string | undefined}[] = [];
    globalThis.fetch = async (url, init) => {
      calls.push({url:String(url), method:init?.method});
      return init?.method === "DELETE" ? reply({...resource, status:"closed"}) : reply([]);
    };
    try {
      requestTerminalClose(); const confirm = confirmDialog();
      terminalSelected.set("other"); await confirm?.run();
      expect(calls.some(c => c.method === "DELETE" && c.url.endsWith("/t"))).toBe(true);
      expect(terminalSelected()).toBe("other");
      expect(calls.some(c => c.url.endsWith("/other/detach"))).toBe(false);
    } finally { confirmDialog.set(null); terminalSelected.set(null); globalThis.fetch = original; }
  });
  test("a late list response cannot replace a newer resource state", async () => {
    setApiBase("http://nexus.test"); currentProject.set("p");
    const original = globalThis.fetch, replies: ((response: Response) => void)[] = [];
    globalThis.fetch = async () => new Promise<Response>(resolve => replies.push(resolve));
    try {
      const first = refreshTerminals(), second = refreshTerminals();
      await tick(); replies[1](reply([{...resource, name:"new"}])); await second;
      replies[0](reply([{...resource, name:"old"}])); await first;
      expect(terminalItems()[0].name).toBe("new");
    } finally { globalThis.fetch = original; }
  });
  test("replays gap and output, takes control and serializes inputs", async () => {
    setApiBase("http://nexus.test"); terminalItems.set([resource]); terminalError.set(""); terminalNotice.set("");
    const original = globalThis.fetch;
    const calls: any[] = [], screen: any[] = [];
    let release!: (value: Response) => void;
    let releaseResize!: (value: Response) => void;
    let screenAck!: () => void;
    let first = true, retry = true, holdResize = false;
    globalThis.fetch = async (url, init) => {
      const path = String(url), body = init?.body ? JSON.parse(String(init.body)) : {};
      calls.push({path,body,method:init?.method});
      if (path.includes("/stream?")) {
        const chunk = new TextEncoder().encode('event: gap\ndata: {"version":1,"type":"gap","seq":2,"rows":24,"cols":80}\n\nid: 3\nevent: output\ndata: {"version":1,"type":"output","seq":3,"data":"aGk="}\n\nid: 3\nevent: output\ndata: {"version":1,"type":"output","seq":3,"data":"aGk="}\n\nid: 4\nevent: resize\ndata: {"version":1,"type":"resize","seq":4,"rows":30,"cols":100}\n\n');
        let sent = false;
        return {ok:true,status:200,body:{getReader:() => ({read:() => sent ? new Promise(() => {}) : (sent = true, Promise.resolve({done:false,value:chunk})),cancel:async () => {}})}} as any;
      }
      if (path.endsWith("/attach")) return reply({attachment_id:"a"});
      if (path.endsWith("/control")) return reply({...resource,controller:"test"});
      if (path.endsWith("/resize") && holdResize) { holdResize = false; return new Promise<Response>(r => { releaseResize = r; }); }
      if (path.endsWith("/resize")) return reply(resource);
      if (path.endsWith("/input") && first) { first = false; return new Promise<Response>(r => { release = r; }); }
      if (path.endsWith("/input") && body.input_seq === 3 && retry) { retry = false; throw new Error("connection lost"); }
      if (path.endsWith("/input")) return reply({input_seq:body.input_seq});
      return reply({});
    };
    setTerminalSender(m => { screen.push(m); if (m.type === "write") return new Promise<void>(resolve => { screenAck = resolve; }); });
    try {
      await selectTerminal("t"); await tick();
      expect(terminalNotice()).toContain("Terminal history was discarded");
      expect(screen.some(m => m.type === "write" && m.data === "aGk=")).toBe(true);
      screenAck(); await tick();
      expect(screen.filter(m => m.type === "write").length).toBe(1);
      expect(screen.some(m => m.type === "dimensions" && m.rows === 30 && m.cols === 100)).toBe(true);
      expect(terminalControlled()).toBe(true);
      holdResize = true; resizeTerminal(30, 100); resizeTerminal(40, 110); await tick();
      expect(calls.filter(c => c.path.endsWith("/resize")).map(c => [c.body.rows, c.body.cols])).toEqual([[24,80],[30,100]]);
      releaseResize(reply(resource)); await tick();
      expect(calls.filter(c => c.path.endsWith("/resize")).map(c => [c.body.rows, c.body.cols])).toEqual([[24,80],[30,100],[40,110]]);
      inputTerminal("a"); inputTerminal("b"); await tick();
      expect(calls.filter(c => c.path.endsWith("/input")).length).toBe(1);
      await controlTerminal("release");
      await selectTerminal("other");
      expect(terminalError()).toBe("Wait for terminal input to be acknowledged");
      expect(calls.filter(c => c.path.endsWith("/control")).length).toBe(1);
      expect(calls.filter(c => c.path.endsWith("/attach")).length).toBe(1);
      release(reply({input_seq:1})); await tick();
      expect(calls.filter(c => c.path.endsWith("/input")).map(c => c.body.input_seq)).toEqual([1,2]);
      inputTerminal("c"); await tick();
      const inputCalls = calls.filter(c => c.path.endsWith("/input"));
      expect(inputCalls.map(c => c.body.input_seq)).toEqual([1,2,3,3]);
      expect(inputCalls[2].body.data).toBe(inputCalls[3].body.data);
      closeTerminalPanel(); await tick();
      expect(calls.some(c => c.path.endsWith("/detach"))).toBe(true);
      expect(calls.some(c => c.path.endsWith("/t") && c.method === "DELETE")).toBe(false);
    } finally { closeTerminalPanel(); setTerminalSender(null); globalThis.fetch = original; }
  });
});
