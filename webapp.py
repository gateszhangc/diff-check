#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sitediff 本地 Web 控制台。

用法：
  python3 webapp.py

在浏览器打开 http://127.0.0.1:8080/ ，即可从页面输入网站并发起扫描。
"""

import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "sitediff.py"
REPORTS = ROOT / "reports"
SNAPSHOTS = ROOT / "snapshots"
# 本地默认只监听回环地址；容器里用 SITEDIFF_HOST=0.0.0.0 / SITEDIFF_PORT=3000 覆盖
HOST = os.environ.get("SITEDIFF_HOST", "127.0.0.1")
PORT = int(os.environ.get("SITEDIFF_PORT", "8080"))

# 只允许同时跑一个扫描，避免两个任务争抢同一个 vN 版本号。
JOB_LOCK = threading.Lock()
JOB = {
    "id": None,
    "status": "idle",  # idle / running / success / failed / canceled
    "url": "",
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "version": None,
    "report": None,
    "message": "",
    "log": "",
}


def append_log(text):
    with JOB_LOCK:
        JOB["log"] = (JOB["log"] + text).lstrip("\n")
        # 防止异常站点输出把内存撑爆；正常扫描远小于这个量。
        if len(JOB["log"]) > 500_000:
            JOB["log"] = "…输出过长，已截断…\n" + JOB["log"][-500_000:]


def set_job(**kwargs):
    with JOB_LOCK:
        JOB.update(kwargs)


def public_job():
    with JOB_LOCK:
        return {key: value for key, value in JOB.items() if key != "process"}


def normalize_url(value):
    value = (value or "").strip()
    if not value:
        raise ValueError("请输入网站 URL")
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError("URL 格式不正确，示例：https://example.com/")
    if not parsed.path:
        value += "/"
    return value


def snapshot_versions():
    if not SNAPSHOTS.exists():
        return []
    result = []
    for item in SNAPSHOTS.iterdir():
        match = re.fullmatch(r"v(\d+)", item.name)
        if match and (item / "seo.json").is_file():
            result.append((int(match.group(1)), item))
    return sorted(result, key=lambda x: x[0])


def read_base_url(version_dir):
    try:
        with (version_dir / "meta.json").open(encoding="utf-8") as f:
            return json.load(f).get("base_url", "")
    except Exception:
        return ""


def same_host(a, b):
    try:
        return urllib.parse.urlsplit(a).hostname == urllib.parse.urlsplit(b).hostname
    except Exception:
        return False


def run_command(args, phase):
    append_log("\n【%s】%s\n" % (phase, " ".join(args)))
    proc = subprocess.Popen(
        args,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    set_job(process=proc)
    try:
        for line in proc.stdout:
            append_log(line)
        returncode = proc.wait()
    finally:
        set_job(process=None)
    append_log("【%s结束】退出码 %s\n" % (phase, returncode))
    return returncode


def scan_worker(url, max_pages, screenshot):
    try:
        before = {v for v, _ in snapshot_versions()}
        args = [sys.executable, "-u", str(SCRIPT), "snapshot", url]
        if max_pages is not None:
            args += ["--max-pages", str(max_pages)]
        if not screenshot:
            args.append("--no-screenshot")

        append_log("页面上限: %s\n" % ("不限（抓取 sitemap 中的全部页面）"
                                      if max_pages is None else "%d 页" % max_pages))
        rc = run_command(args, "抓取快照")
        if rc != 0:
            set_job(status="failed", returncode=rc, finished_at=time.time(),
                    message="扫描失败：sitediff.py 返回码 %s" % rc)
            return

        versions = snapshot_versions()
        new_versions = [v for v, _ in versions if v not in before]
        if not new_versions:
            set_job(status="failed", returncode=rc, finished_at=time.time(),
                    message="扫描进程成功结束，但没有找到新快照。")
            return

        new_version = max(new_versions)
        new_dir = SNAPSHOTS / ("v%d" % new_version)
        new_base = read_base_url(new_dir)

        # 找同域名下最近的历史快照，自动生成对比报告。
        previous = None
        for version, directory in reversed(versions):
            if version >= new_version:
                continue
            if same_host(read_base_url(directory), new_base):
                previous = (version, directory)
                break

        if previous is None:
            set_job(status="success", returncode=0, finished_at=time.time(),
                    version=new_version, report=None,
                    message="v%d 基线已建立。首次扫描不会生成对比报告；网站修改后再次输入同一网址扫描，会自动生成版本对比报告。" % new_version)
            return

        old_version = previous[0]
        report_name = "v%d-v%d.html" % (old_version, new_version)
        rc = run_command(
            [sys.executable, "-u", str(SCRIPT), "compare",
             "v%d" % old_version, "v%d" % new_version],
            "生成对比报告",
        )
        report_path = REPORTS / report_name
        if rc != 0 or not report_path.is_file():
            set_job(status="failed", returncode=rc, finished_at=time.time(),
                    version=new_version, report=None,
                    message="快照 v%d 已保存，但对比报告生成失败。" % new_version)
            return

        set_job(status="success", returncode=0, finished_at=time.time(),
                version=new_version, report="/" + report_name,
                message="扫描完成：已生成 v%d → v%d 对比报告。" % (old_version, new_version))
    except Exception as exc:
        set_job(status="failed", finished_at=time.time(),
                message="扫描异常：%s" % exc)
        append_log("\n【异常】%s\n" % exc)


def report_items():
    REPORTS.mkdir(exist_ok=True)
    items = []
    for path in REPORTS.glob("v*-v*.html"):
        match = re.fullmatch(r"v(\d+)-v(\d+)\.html", path.name)
        if not match:
            continue
        title = "v%s → v%s 对比报告" % (match.group(1), match.group(2))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            match_title = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
            if match_title:
                title = re.sub(r"\s+", " ", match_title.group(1)).strip()
        except Exception:
            pass
        items.append({
            "name": path.name,
            "url": "/" + path.name,
            "title": title,
            "mtime": path.stat().st_mtime,
            "size": path.stat().st_size,
        })
    return sorted(items, key=lambda x: x["mtime"], reverse=True)


DASHBOARD = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>sitediff 扫描控制台</title>
<style>
:root{--bg:#f4f6f8;--card:#fff;--text:#172033;--muted:#667085;--line:#e3e8ef;
--brand:#1f56d2;--ok:#16803c;--bad:#c0392b;--warn:#a15c00}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:28px 20px 60px}
.hero{background:#111827;color:#fff;border-radius:16px;padding:28px;margin-bottom:20px}
.hero h1{margin:0 0 8px;font-size:26px}.hero p{margin:0;color:#cbd5e1;line-height:1.7}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px;margin:16px 0;box-shadow:0 1px 2px #1018280d}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
.field{flex:1 1 340px}.field label{display:block;font-size:13px;font-weight:700;margin-bottom:7px}
.small{width:130px}.tiny{font-size:12px;color:var(--muted);margin-top:8px;line-height:1.6}
input[type=url],input[type=number]{width:100%;height:44px;border:1px solid #cbd5e1;border-radius:10px;padding:0 13px;font-size:15px;background:#fff}
input:focus{outline:3px solid #bfdbfe;border-color:var(--brand)}
button{height:44px;border:0;border-radius:10px;padding:0 20px;font-size:15px;font-weight:700;cursor:pointer}
.primary{background:var(--brand);color:#fff}.primary:disabled{background:#94a3b8;cursor:not-allowed}
.ghost{background:#eef2f6;color:#334155}
.check{display:flex;align-items:center;gap:8px;height:44px;font-size:14px;color:#334155}
.status{margin-top:20px;padding:15px 17px;border-radius:11px;background:#f8fafc;border:1px solid var(--line);line-height:1.7}
.status.running{border-color:#bfdbfe;background:#eff6ff}.status.success{border-color:#bbf7d0;background:#f0fdf4}
.status.failed,.status.canceled{border-color:#fecaca;background:#fef2f2}
.pill{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:800;padding:4px 10px;border-radius:999px;background:#e2e8f0;color:#334155}
.dot{width:8px;height:8px;border-radius:50%;background:#64748b}.running .dot{background:var(--brand);animation:pulse 1s infinite}
.success .dot{background:var(--ok)}.failed .dot,.canceled .dot{background:var(--bad)}
@keyframes pulse{50%{opacity:.25}}
pre{white-space:pre-wrap;word-break:break-word;max-height:340px;overflow:auto;background:#0b1220;color:#d5e5ff;border-radius:11px;padding:15px;font-size:12px;line-height:1.65;margin:14px 0 0}
h2{font-size:19px;margin:0 0 14px}.empty{color:var(--muted);line-height:1.8}
.reports{display:grid;gap:12px}.report{display:block;text-decoration:none;color:inherit;background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.report:hover{border-color:var(--brand);box-shadow:0 4px 14px #1f56d21a}.report .t{font-weight:750;margin-bottom:5px}
.report .m{font-size:12px;color:var(--muted)}
.actions{display:flex;gap:10px;margin-top:15px;flex-wrap:wrap}
.error{display:none;margin-top:10px;color:var(--bad);font-size:14px}
</style>
</head>
<body>
<main class="wrap">
  <section class="hero">
    <h1>网站版本快照与 SEO 对比控制台</h1>
    <p>输入网站首页 URL 即可开始扫描。第一次会建立基线；网站修改后再次扫描，会自动生成对比报告。</p>
  </section>

  <section class="card">
    <div class="row">
      <div class="field">
        <label for="url">网站 URL</label>
        <input id="url" type="text" inputmode="url" placeholder="https://example.com/ 或 example.com" autocomplete="off" spellcheck="false">
      </div>
      <div class="field small">
        <label for="max_pages">最多页面</label>
        <input id="max_pages" type="number" min="1" max="5000" placeholder="全部">
      </div>
      <label class="check"><input id="screenshot" type="checkbox" checked>抓取截图</label>
      <button id="start" class="primary" type="button">开始扫描</button>
    </div>
    <div id="form_error" class="error"></div>
    <div class="tiny">提示：「最多页面」留空即抓取 sitemap 中的全部页面。扫描会在后台运行，
      可以留在当前页查看实时输出。截图需要本机安装 Chrome/Chromium。</div>
    <div id="status" class="status" hidden>
      <span class="pill"><span class="dot"></span><span id="state">运行中</span></span>
      <div id="message" style="margin-top:10px"></div>
      <pre id="log" hidden></pre>
      <div class="actions">
        <button id="open_report" class="primary" type="button" hidden>打开对比报告</button>
        <button id="toggle_log" class="ghost" type="button" hidden>显示日志</button>
      </div>
    </div>
  </section>

  <section class="card">
    <h2>报告中心</h2>
    <div id="reports" class="reports"><div class="empty">暂无对比报告。</div></div>
  </section>
</main>
<script>
const stateText={idle:'空闲',running:'扫描中',success:'完成',failed:'失败',canceled:'已取消'};
let showingLog=false;
let latestJob=null;

function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

async function refreshStatus(){
  try{
    const r=await fetch('/api/status',{cache:'no-store'});
    const j=await r.json();
    renderStatus(j.job);
    if(j.job.status!=='running') await refreshReports();
  }catch(e){}
}

function renderStatus(j){
  latestJob=j;
  const box=document.getElementById('status');
  box.hidden=!j || j.status==='idle';
  if(!j || j.status==='idle')return;
  box.className='status '+j.status;
  document.getElementById('state').textContent=stateText[j.status]||j.status;
  document.getElementById('message').textContent=j.message||j.url||'';
  const log=document.getElementById('log');
  log.textContent=j.log||'';
  log.hidden=!showingLog;
  document.getElementById('toggle_log').hidden=!(j.log||'').trim();
  document.getElementById('toggle_log').textContent=showingLog?'收起日志':'显示日志';
  document.getElementById('open_report').hidden=!j.report;
  document.getElementById('start').disabled=j.status==='running';
}

async function refreshReports(){
  try{
    const r=await fetch('/api/reports',{cache:'no-store'});
    const j=await r.json();
    const el=document.getElementById('reports');
    if(!j.reports.length){el.innerHTML='<div class="empty">暂无对比报告。完成第一次扫描后会建立基线；再次扫描后生成报告。</div>';return}
    el.innerHTML=j.reports.map(x=>`<a class="report" href="${esc(x.url)}"><div class="t">${esc(x.title)}</div><div class="m">${esc(new Date(x.mtime*1000).toLocaleString())} · ${(x.size/1024).toFixed(1)} KB</div></a>`).join('');
  }catch(e){}
}

document.getElementById('start').onclick=async()=>{
  const err=document.getElementById('form_error');err.style.display='none';
  const rawMax=document.getElementById('max_pages').value.trim();
  const payload={url:document.getElementById('url').value,max_pages:rawMax===''?null:Number(rawMax),screenshot:document.getElementById('screenshot').checked};
  try{
    const r=await fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const j=await r.json();if(!r.ok)throw new Error(j.error||'启动失败');
    showingLog=false;await refreshStatus();
  }catch(e){err.textContent=e.message;err.style.display='block'}
};
document.getElementById('toggle_log').onclick=()=>{showingLog=!showingLog;refreshStatus()};
document.getElementById('open_report').onclick=()=>{if(latestJob&&latestJob.report)location.href=latestJob.report};

refreshStatus();refreshReports();setInterval(refreshStatus,1500);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "sitediff-web/1.0"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def send_bytes(self, code, content, content_type, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def send_json(self, code, data):
        self.send_bytes(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        if path in ("/", "/index.html", "/reports", "/reports/"):
            self.send_bytes(200, DASHBOARD.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self.send_json(200, {"job": public_job()})
            return
        if path == "/api/reports":
            self.send_json(200, {"reports": report_items()})
            return
        if path == "/robots.txt":
            host = self.headers.get("Host", "")
            body = "User-agent: *\nAllow: /\n"
            if host:
                body += "\nSitemap: https://%s/sitemap.xml\n" % host
            self.send_bytes(200, body.encode("utf-8"), "text/plain; charset=utf-8")
            return
        if path == "/sitemap.xml":
            host = self.headers.get("Host", "")
            urls = ["/"] + [x["url"] for x in report_items()]
            body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                    + "".join("  <url><loc>https://%s%s</loc></url>\n" % (host, u) for u in urls)
                    + "</urlset>\n")
            self.send_bytes(200, body.encode("utf-8"), "application/xml; charset=utf-8")
            return

        # 只对外暴露 reports/ 目录，避免泄露源码与快照
        target = (REPORTS / path.lstrip("/")).resolve()
        try:
            target.relative_to(REPORTS.resolve())
        except ValueError:
            self.send_json(403, {"error": "禁止访问"})
            return
        if not target.is_file():
            self.send_json(404, {"error": "文件不存在"})
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self.send_bytes(200, target.read_bytes(), content_type)

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/api/scan":
            self.send_json(404, {"error": "接口不存在"})
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 10_000)
            body = json.loads(self.rfile.read(length) or b"{}")
            url = normalize_url(body.get("url"))
            raw_max = body.get("max_pages")
            if raw_max is None or str(raw_max).strip() in ("", "0"):
                max_pages = None  # 留空 = 不限制，抓取 sitemap 中的全部页面
            else:
                max_pages = int(raw_max)
                if not 1 <= max_pages <= 5000:
                    raise ValueError("最多页面数必须在 1-5000 之间，或留空抓取全部页面")
            screenshot = bool(body.get("screenshot", True))
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})
            return

        current = public_job()
        if current["status"] == "running":
            self.send_json(409, {"error": "已有扫描正在运行，请等待完成。"})
            return

        job_id = uuid.uuid4().hex[:12]
        with JOB_LOCK:
            JOB.update({
                "id": job_id,
                "status": "running",
                "url": url,
                "started_at": time.time(),
                "finished_at": None,
                "returncode": None,
                "version": None,
                "report": None,
                "message": "正在初始化扫描…",
                "log": "",
                "process": None,
            })
        threading.Thread(target=scan_worker, args=(url, max_pages, screenshot), daemon=True).start()
        self.send_json(202, {"id": job_id, "status": "running"})


def main():
    REPORTS.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("sitediff Web 控制台已启动: http://%s:%d/" % (HOST, PORT), flush=True)
    print("按 Ctrl+C 停止服务。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
