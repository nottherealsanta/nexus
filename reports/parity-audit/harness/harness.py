#!/usr/bin/env python3
"""A tiny terminal host that answers OSC 10/11 like Ghostty. Usage:
  harness.py serve NAME COLSxROWS -- cmd args...   (background)
  harness.py send NAME keys...  | dump NAME out.ans | stop NAME
Keys: literal text, or <name> tokens: <esc> <enter> <tab> <up> <down> <left> <right> <bs> <c-k> <c-comma> <space> <pgup> <pgdn> <home> <end> <s-tab> <s-right>"""
import os, sys, pty, select, socket, struct, fcntl, termios, re, time, signal, threading
import pyte
SOCK="/tmp/hz-%s.sock"
FG="cccccc"; BG="0b0b0b"
KEYS={"esc":"\x1b","enter":"\r","tab":"\t","up":"\x1b[A","down":"\x1b[B","right":"\x1b[C","left":"\x1b[D","bs":"\x7f","space":" ",
"pgup":"\x1b[5~","pgdn":"\x1b[6~","home":"\x1b[H","end":"\x1b[F","s-tab":"\x1b[Z","s-right":"\x1b[1;2C","c-k":"\x0b","c-comma":"\x1b[44;5u","c-c":"\x03","c-u":"\x15"}
def expand(arg):
    out=""
    for part in re.split(r"(<[a-z0-9-]+>)",arg):
        m=re.fullmatch(r"<([a-z0-9-]+)>",part)
        out+= KEYS[m.group(1)] if m else part
    return out
def serve(name, size, cmd):
    cols,rows=map(int,size.split("x"))
    pid,fd=pty.fork()
    if pid==0:
        os.environ["TERM"]="xterm-256color"; os.environ["COLORTERM"]="truecolor"; os.environ["TERM_PROGRAM"]="ghostty"
        os.execvp(cmd[0],cmd)
    fcntl.ioctl(fd,termios.TIOCSWINSZ,struct.pack("HHHH",rows,cols,cols*8,rows*16))
    screen=pyte.Screen(cols,rows); stream=pyte.ByteStream(screen)
    lock=threading.Lock(); alive=[True]; appcur=[False]
    QUERY=re.compile(rb"\x1b\](10|11);\?(?:\x07|\x1b\\)|\x1b\[c|\x1b\[0c|\x1b\[>q|\x1b\[\?u|\x1b\[6n|\x1b\[14t|\x1b\[16t|\x1b\[18t|\x1b\[\?(\d+)\$p")
    DROP=re.compile(rb"\x1b\[[<>=?][0-9;]*u|\x1b\[[<>=]?[0-9;]*q|\x1b\[\?(?:2026|1004|2004|1007|1000|1002|1003|1015|1006)[hl]")
    def reply(data):
        for m in QUERY.finditer(data):
            s=m.group(0)
            if s.startswith(b"\x1b]10"): os.write(fd,("\x1b]10;rgb:%s/%s/%s\x1b\\"%(FG[0:2]*2,FG[2:4]*2,FG[4:6]*2)).encode())
            elif s.startswith(b"\x1b]11"): os.write(fd,("\x1b]11;rgb:%s/%s/%s\x1b\\"%(BG[0:2]*2,BG[2:4]*2,BG[4:6]*2)).encode())
            elif s in (b"\x1b[c",b"\x1b[0c"): os.write(fd,b"\x1b[?62;4c")
            elif s==b"\x1b[>q": os.write(fd,b"\x1bP>|ghostty 1.2.0\x1b\\")
            elif s==b"\x1b[?u": os.write(fd,b"\x1b[?0u")
            elif s==b"\x1b[6n":
                with lock: os.write(fd,("\x1b[%d;%dR"%(screen.cursor.y+1,screen.cursor.x+1)).encode())
            elif s==b"\x1b[14t": os.write(fd,("\x1b[4;%d;%dt"%(rows*16,cols*8)).encode())
            elif s==b"\x1b[16t": os.write(fd,b"\x1b[6;16;8t")
            elif s==b"\x1b[18t": os.write(fd,("\x1b[8;%d;%dt"%(rows,cols)).encode())
            elif s.endswith(b"$p"): os.write(fd,("\x1b[?%s;2$y"%m.group(2).decode()).encode())
    def pump():
        buf=b""
        while alive[0]:
            r,_,_=select.select([fd],[],[],0.2)
            if fd in r:
                try: data=os.read(fd,65536)
                except OSError: alive[0]=False; break
                if not data: alive[0]=False; break
                reply(data)
                if b'\x1b[?1h' in data: appcur[0]=True
                if b'\x1b[?1l' in data: appcur[0]=False
                with lock:
                    try: stream.feed(DROP.sub(b"",data))
                    except Exception as e: pass
    threading.Thread(target=pump,daemon=True).start()
    p=SOCK%name
    if os.path.exists(p): os.unlink(p)
    srv=socket.socket(socket.AF_UNIX); srv.bind(p); srv.listen(4)
    def dump():
        out=[]
        with lock:
            for y in range(rows):
                line=screen.buffer[y]; s=""; last=None
                for x in range(cols):
                    c=line[x]
                    if c.data=="": continue
                    key=(c.fg,c.bg,c.bold,c.italics,c.underscore,c.reverse)
                    if key!=last:
                        sg=["0"]
                        def col(v,base):
                            if v=="default": return None
                            names=["black","red","green","brown","blue","magenta","cyan","white"]
                            if v in names: return str(base+names.index(v))
                            if v.startswith("bright"): return str(base+60+["black","red","green","brown","blue","magenta","cyan","white"].index(v[6:]))
                            if re.fullmatch(r"[0-9a-f]{6}",v): return "%d;2;%d;%d;%d"%(base+8,int(v[0:2],16),int(v[2:4],16),int(v[4:6],16))
                            return None
                        f=col(c.fg,30); b=col(c.bg,40)
                        if f: sg.append(f)
                        if b: sg.append(b)
                        if c.bold: sg.append("1")
                        if c.italics: sg.append("3")
                        if c.underscore: sg.append("4")
                        if c.reverse: sg.append("7")
                        s+="\x1b["+";".join(sg)+"m"; last=key
                    s+=c.data
                out.append(s+"\x1b[0m")
        return "\n".join(out)
    while alive[0]:
        srv.settimeout(0.5)
        try: conn,_=srv.accept()
        except socket.timeout: continue
        data=conn.makefile("rb").readline().decode()
        verb,_,rest=data.rstrip("\n").partition(" ")
        if verb=="send":
            raw=rest.encode().decode("unicode_escape").encode("latin1")
            if raw in (b"\x1b[A",b"\x1b[B",b"\x1b[C",b"\x1b[D",b"\x1b[H",b"\x1b[F"): raw=b"\x1bO"+raw[-1:]
            os.write(fd,raw); conn.sendall(b"ok\n")
        elif verb=="dump":
            open(rest,"w").write(dump()); conn.sendall(b"ok\n")
        elif verb=="text":
            with lock: t="\n".join(l.rstrip() for l in screen.display)
            conn.sendall(t.encode()+b"\n")
        elif verb=="stop":
            conn.sendall(b"ok\n"); alive[0]=False
        conn.close()
    try: os.kill(pid,signal.SIGTERM)
    except Exception: pass
    os.unlink(p)
def client(verb, name, rest=""):
    s=socket.socket(socket.AF_UNIX); s.connect(SOCK%name); s.sendall((verb+" "+rest+"\n").encode())
    data=b""
    while True:
        d=s.recv(65536)
        if not d: break
        data+=d
    return data.decode()
if __name__=="__main__":
    a=sys.argv[1:]
    if a[0]=="serve":
        i=a.index("--"); serve(a[1],a[2],a[i+1:])
    elif a[0]=="send":
        for k in a[2:]:
            seq=expand(k)
            client("send",a[1],seq.encode("unicode_escape").decode()); time.sleep(0.35)
    elif a[0]=="dump": client("dump",a[1],a[2])
    elif a[0]=="text": print(client("text",a[1]),end="")
    elif a[0]=="stop": print(client("stop",a[1]),end="")
