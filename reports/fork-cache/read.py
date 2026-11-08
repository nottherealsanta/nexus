import json, os, sys
from pathlib import Path
for f in sys.argv[1:]:
    t = open(f).read(); d = json.loads(t[t.find('{'):])
    os.environ["NEXUS10_HOME"] = d["scratch"] + "/home"
    from nexus.store import Store
    st = Store(Path(d["scratch"]) / "nexus.db")
    print("==", d["model"])
    for s in d["sessions"]:
        role = "fork  " if s["fork"] else "parent"
        for e in st.events(s["id"], limit=5000, types=["usage"]):
            print(" ", role, json.dumps(e.get("data"))[:300])
