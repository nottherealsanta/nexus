from test_server import setup  # noqa: F401 - the shared server fixture

from nexus.perf import Perf


async def test_requests_carry_server_timing_and_echo_the_trace(setup):  # noqa: F811
    _, client, _, _ = setup
    response = await client.get("/api/info", headers={"x-nexus-trace": "tui-7"})
    assert response.headers["x-nexus-trace"] == "tui-7"
    assert response.headers["server-timing"].startswith("app;dur=")
    report = (await client.get("/api/debug/perf")).json()
    info = next(r for r in report["routes"] if r["route"] == "GET info")
    assert info["count"] == 1 and info["mean_bytes"] > 0
    assert {"loop", "slow", "stalls", "inflight", "cpu_percent"} <= set(report)


async def test_errors_are_counted_and_timed(setup):  # noqa: F811
    _, client, _, _ = setup
    assert (await client.get("/api/session/nope/turn")).status_code == 404
    report = (await client.get("/api/debug/perf")).json()
    turns = next(r for r in report["routes"] if r["route"] == "GET turns")
    assert turns["count"] == 1 and turns["errors"] == 0  # a 404 is an answer, not a failure


def test_slow_requests_and_stalls_name_what_was_in_flight():
    perf = Perf(slow_ms=0)
    token = perf.begin("messages", "tui-1")
    perf.tick(300)  # the loop woke 300 ms late while `messages` was open
    perf.end(token, method="GET", status=200, size=10)
    report = perf.json()
    assert report["slow"][0]["route"] == "GET messages" and report["slow"][0]["trace"] == "tui-1"
    stall = report["stalls"][0]
    assert stall["late_ms"] == 300 and stall["inflight"][0]["route"] == "messages"
    assert report["loop"]["late"] == {"100ms": 1, "250ms": 1, "1s": 0}
    assert report["inflight"] == []


def test_short_lateness_is_not_a_stall():
    perf = Perf()
    perf.tick(5)
    assert perf.json()["stalls"] == [] and perf.lag_max_ms == 5


def test_percentiles_come_from_recent_requests():
    perf = Perf(slow_ms=10_000)
    for _ in range(10):
        token = perf.begin("turns", None)
        perf.end(token, method="GET", status=200, size=100)
    (route,) = perf.json()["routes"]
    assert route["count"] == 10 and route["p95_ms"] >= route["p50_ms"] >= 0


def test_render_puts_core_and_tui_side_by_side(tmp_path):
    perf = Perf(slow_ms=0)
    token = perf.begin("turns", "tui-1-4")
    perf.tick(400)
    perf.end(token, method="GET", status=200, size=2048)
    log = tmp_path / "tui-perf.jsonl"
    log.write_text(
        '{"kind":"history","at":1,"ms":3200.5,"session":"ses_1","messages":200,"turns":1800,'
        '"phases":{"first_ms":410.0,"older_pages":8,"older_ms":2790.5}}\n'
        'not json\n'
        '{"kind":"request","at":2,"method":"GET","route":"/api/session/:id/turn","ms":900,'
        '"core_ms":120,"outside_core_ms":780,"bytes":4000}\n')
    from nexus.perf import read_client_log, render
    lines = read_client_log(log)
    assert len(lines) == 2
    text = render(perf.json(), lines)
    assert "GET turns" in text and "400 ms late · turns" in text
    assert "8 older pages 2790.5 ms" in text and "outside      780" in text
    assert "daemon not running" in render(None, [])
