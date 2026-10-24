# Author: Kai0421
import importlib.util
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "dashboard" / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_render_embeds_snapshot_and_escapes_script_close():
    build_page = load("build_page")
    snap = {"account": {"equity": "100000"}, "note": "</script><b>x</b>"}
    html = build_page.render(snap, '<script id="data" type="application/json">__DATA__</script>')
    body = html.split('type="application/json">', 1)[1].rsplit("</script>", 1)[0]
    assert "</script>" not in body
    assert json.loads(body) == snap


def test_template_has_placeholder_and_live_server():
    template = (ROOT / "scripts" / "dashboard" / "template.html").read_text(encoding="utf-8")
    assert "__DATA__" in template
    assert "host:tradingbot" in template and "dashboard_snapshot" in template


def test_snapshot_db_reads_tables(tmp_path, monkeypatch):
    snapshot = load("snapshot")
    (tmp_path / "data").mkdir()
    con = sqlite3.connect(tmp_path / "data" / "bot.db")
    con.execute("create table trades (symbol text, pnl real)")
    con.execute("insert into trades values ('EMR', 12.5)")
    con.commit()
    con.close()
    monkeypatch.chdir(tmp_path)
    out = snapshot.db()
    assert out["trades"] == [{"symbol": "EMR", "pnl": 12.5}]
    assert "error" in out["signals"]  # missing table is reported, not raised


def test_snapshot_only_uses_get_requests():
    src = (ROOT / "scripts" / "dashboard" / "snapshot.py").read_text()
    assert "method=" not in src and "POST" not in src and "DELETE" not in src
    assert "paper-api.alpaca.markets" in src
