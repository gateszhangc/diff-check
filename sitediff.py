#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sitediff.py — 网站版本快照与 SEO 对比工具（零第三方依赖）

用法:
  python3 sitediff.py snapshot <URL> [--max-pages N] [--no-screenshot] [--commit]
  python3 sitediff.py compare v1 v2 [--output FILE]
  python3 sitediff.py compare --auto
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser

TOOL_VERSION = "1.0"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) SiteDiffBot/" + TOOL_VERSION
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]
BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th", "div",
    "section", "article", "header", "footer", "nav", "main", "aside",
    "figure", "figcaption", "tr", "table", "blockquote",
}
SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css", ".js",
    ".pdf", ".zip", ".gz", ".tar", ".mp4", ".mp3", ".woff", ".woff2", ".ttf", ".eot",
}


# ---------------------------------------------------------------------------
# HTML 解析（标准库实现，提取 SEO 相关数据）
# ---------------------------------------------------------------------------

class SEOParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._title_buf = []
        self.metas = []            # <meta> 原始属性
        self.link_tags = []        # <link> 原始属性
        self.json_ld_raw = []      # JSON-LD 原始文本
        self.headings = {"h%d" % i: [] for i in range(1, 7)}
        self._heading_tag = None
        self._heading_buf = []
        self.images = []           # {src, alt}
        self.anchor_hrefs = []     # <a href> 原始值
        self.html_lang = None
        self.text_lines = []       # 可见文本（按块级元素分行）
        self._line_buf = []
        self._skip_depth = 0       # script/style 内部跳过
        self._in_ld = False
        self._ld_buf = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        tag = tag.lower()
        if tag == "html" and "lang" in a:
            self.html_lang = a.get("lang") or None
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            self.metas.append(a)
        elif tag == "link":
            self.link_tags.append(a)
        elif tag in ("script", "style"):
            self._skip_depth += 1
            if tag == "script" and "ld+json" in (a.get("type") or "").lower():
                self._in_ld = True
                self._ld_buf = []
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading_tag = tag
            self._heading_buf = []
        elif tag == "img":
            self.images.append({
                "src": a.get("src") or a.get("data-src") or "",
                "alt": a.get("alt"),
            })
        elif tag == "a":
            href = a.get("href")
            if href:
                self.anchor_hrefs.append(href)
        elif tag == "br":
            self._flush_line()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
            self.title = re.sub(r"\s+", " ", "".join(self._title_buf)).strip()
        elif tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
            if tag == "script" and self._in_ld:
                self._in_ld = False
                self.json_ld_raw.append("".join(self._ld_buf))
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            if self._heading_tag == tag:
                text = re.sub(r"\s+", " ", unescape("".join(self._heading_buf))).strip()
                self.headings[tag].append(text)
                self._heading_tag = None
                self._heading_buf = []
            self._flush_line()
        elif tag in BLOCK_TAGS:
            self._flush_line()

    def handle_data(self, data):
        if self._skip_depth > 0:
            if self._in_ld:
                self._ld_buf.append(data)
            return
        if self._in_title:
            self._title_buf.append(data)
            return
        if self._heading_tag:
            self._heading_buf.append(data)
        self._line_buf.append(data)

    def _flush_line(self):
        text = re.sub(r"\s+", " ", unescape("".join(self._line_buf))).strip()
        if text:
            if not self.text_lines or self.text_lines[-1] != text:
                self.text_lines.append(text)
        self._line_buf = []


def parse_html(html_text):
    """解析 HTML，返回 SEOParser 结果"""
    p = SEOParser()
    try:
        p.feed(html_text)
        p.close()
    except Exception:
        pass  # 容忍残缺 HTML
    p._flush_line()
    return p


# ---------------------------------------------------------------------------
# 抓取与工具函数
# ---------------------------------------------------------------------------

def fetch(url, timeout=25):
    """抓取 URL，返回 (text, status, final_url, content_type)；失败时 text=None"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = getattr(r, "status", 200)
            ctype = r.headers.get("Content-Type", "")
            final_url = r.geturl()
    except urllib.error.HTTPError as e:
        try:
            ctype = e.headers.get("Content-Type", "") if e.headers else ""
        except Exception:
            ctype = ""
        return None, e.code, url, ctype
    except Exception as e:
        return None, -1, url, str(e)
    enc = None
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    if m:
        enc = m.group(1)
    if not enc:
        m2 = re.search(rb"""<meta[^>]+charset=["']?([\w-]+)""", raw[:4096], re.I)
        if m2:
            enc = m2.group(1).decode("ascii", "ignore")
    text = raw.decode(enc or "utf-8", errors="replace")
    return text, status, final_url, ctype


def norm_url(u):
    """归一化 URL 身份：小写 host、去默认端口、去 fragment、去末尾斜杠（根除外）"""
    try:
        p = urllib.parse.urlsplit(u)
    except Exception:
        return u
    scheme = (p.scheme or "http").lower()
    netloc = p.netloc.lower()
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    if scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]
    path = p.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urllib.parse.urlunsplit((scheme, netloc, path, p.query, ""))


def slug_for(url):
    """把 URL 转成安全的文件名（不含扩展名）"""
    p = urllib.parse.urlsplit(url)
    path = (p.path or "/").strip("/")
    name = path.replace("/", "-") if path else "index"
    if p.query:
        name += "-" + hashlib.md5(p.query.encode()).hexdigest()[:6]
    name = re.sub(r"[^\w.-]+", "-", name).strip("-")
    return (name or "index")[:80]


def is_html_content(ctype, text):
    if text is None:
        return False
    ct = (ctype or "").lower()
    if "html" in ct or "xml" in ct:
        return True
    return bool(re.search(r"<(html|body|head|meta|title|div|main)\b", text[:5000], re.I))


def parse_sitemap(xml_text, max_urls=500):
    """解析 sitemap.xml，返回其中 loc URL 列表"""
    urls = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return urls
    for el in root.iter():
        local = el.tag.split("}")[-1].lower()
        if local == "loc" and el.text and el.text.strip():
            u = el.text.strip()
            if u.startswith("http"):
                urls.append(u)
                if len(urls) >= max_urls:
                    break
    return urls


def sample_sitemap_pages(base, robots_text, max_pages):
    """从 robots.txt 的 Sitemap 声明与 /sitemap.xml 抽样页面"""
    sitemap_urls = []
    if robots_text:
        for line in robots_text.splitlines():
            m = re.match(r"\s*sitemap\s*:\s*(\S+)", line, re.I)
            if m:
                sitemap_urls.append(m.group(1))
    default_sm = urllib.parse.urljoin(base, "/sitemap.xml")
    if default_sm not in sitemap_urls:
        sitemap_urls.insert(0, default_sm)

    candidates = []
    for sm in sitemap_urls[:3]:
        text, status, _, _ = fetch(sm, timeout=15)
        if text is None or status != 200:
            continue
        found = parse_sitemap(text)
        # sitemap index：递归一层
        if found and all(re.search(r"sitemap", urllib.parse.urlsplit(f).path, re.I) for f in found[:5]):
            for sub in found[:3]:
                t2, s2, _, _ = fetch(sub, timeout=15)
                if t2 and s2 == 200:
                    candidates.extend(parse_sitemap(t2))
        else:
            candidates.extend(found)

    host = urllib.parse.urlsplit(base).netloc.lower()
    pages = []
    seen = set()
    for u in candidates:
        p = urllib.parse.urlsplit(u)
        if p.netloc.lower() != host:
            continue
        path = (p.path or "/").lower()
        if os.path.splitext(path)[1] in SKIP_EXTENSIONS:
            continue
        n = norm_url(u)
        if n in seen:
            continue
        seen.add(n)
        pages.append((len(path.strip("/").split("/")), len(path), n))
    pages.sort()
    return [u for _, _, u in pages[: max(0, max_pages)]]


def find_chrome():
    for c in CHROME_CANDIDATES:
        if os.path.exists(c) and os.access(c, os.X_OK):
            return c
    for name in ("google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def take_screenshot(url, out_path, timeout=60):
    chrome = find_chrome()
    if not chrome:
        return False, "未找到 Chrome/Chromium"
    try:
        subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-first-run",
             "--hide-scrollbars", "--window-size=1280,2400",
             "--screenshot=" + out_path, "--virtual-time-budget=6000", url],
            timeout=timeout, capture_output=True,
        )
    except Exception as e:
        return False, str(e)
    ok = os.path.exists(out_path) and os.path.getsize(out_path) > 1000
    return ok, (None if ok else "截图失败或文件为空")


def next_version(snap_root):
    n = 0
    if os.path.isdir(snap_root):
        for d in os.listdir(snap_root):
            m = re.match(r"^v(\d+)$", d)
            if m:
                n = max(n, int(m.group(1)))
    return n + 1


def latest_versions(snap_root, count=2):
    vs = []
    if os.path.isdir(snap_root):
        for d in os.listdir(snap_root):
            m = re.match(r"^v(\d+)$", d)
            if m:
                vs.append(int(m.group(1)))
    vs.sort(reverse=True)
    return vs[:count]


# ---------------------------------------------------------------------------
# snapshot 子命令
# ---------------------------------------------------------------------------

def extract_page_seo(url, html_text):
    """从 HTML 提取结构化 SEO 数据"""
    p = parse_html(html_text)

    # meta 分类：name -> 普通 meta；property/name -> OG / Twitter
    meta_by_name = {}
    meta_by_property = {}
    for a in p.metas:
        name = (a.get("name") or "").lower()
        prop = (a.get("property") or "").lower()
        if name:
            meta_by_name[name] = a.get("content", "")
        if prop:
            meta_by_property[prop] = a.get("content", "")

    # link: canonical / hreflang
    canonical = None
    hreflang = []
    for a in p.link_tags:
        rel = (a.get("rel") or "").lower()
        if "canonical" in rel and a.get("href"):
            canonical = urllib.parse.urljoin(url, a["href"])
        elif "alternate" in rel and a.get("hreflang"):
            hreflang.append({
                "hreflang": a["hreflang"],
                "href": urllib.parse.urljoin(url, a.get("href", "")),
            })

    # JSON-LD
    json_ld = []
    for raw in p.json_ld_raw:
        try:
            json_ld.append(json.loads(raw))
        except Exception:
            json_ld.append({"_parse_error": raw[:500]})

    # 链接分类（严格同 host 视为内链）
    host = urllib.parse.urlsplit(url).netloc.lower()
    internal, external = set(), set()
    for href in p.anchor_hrefs:
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absu = urllib.parse.urljoin(url, href)
        absn = norm_url(absu)
        if urllib.parse.urlsplit(absn).netloc.lower() == host:
            internal.add(absn)
        elif absn.startswith("http"):
            external.add(absn)

    # 图片与 alt 覆盖率
    images = []
    for img in p.images:
        if not img["src"] or img["src"].startswith("data:"):
            continue
        images.append({"src": urllib.parse.urljoin(url, img["src"]), "alt": img["alt"]})
    alt_total = len(images)
    alt_with = sum(1 for i in images if i["alt"] and i["alt"].strip())

    joined = " ".join(p.text_lines)
    return {
        "title": p.title,
        "meta_description": meta_by_name.get("description"),
        "meta_robots": meta_by_name.get("robots"),
        "canonical": canonical,
        "hreflang": sorted(hreflang, key=lambda x: (x["hreflang"], x["href"])),
        "html_lang": p.html_lang,
        "open_graph": dict(sorted(
            (k, v) for k, v in meta_by_property.items() if k.startswith("og:")
        )),
        "twitter_cards": dict(sorted(
            (k, v) for k, v in list(meta_by_name.items()) + list(meta_by_property.items())
            if k.startswith("twitter:")
        )),
        "json_ld": json_ld,
        "headings": p.headings,
        "images": sorted(images, key=lambda i: i["src"]),
        "images_alt_coverage": round(alt_with / alt_total, 3) if alt_total else None,
        "links": {
            "internal": sorted(internal),
            "external": sorted(external),
        },
        "text_lines": p.text_lines,
        "word_count": len(joined.split()),
    }


def cmd_snapshot(args):
    root = os.path.dirname(os.path.abspath(__file__))
    snap_root = os.path.abspath(args.output_dir) if args.output_dir else os.path.join(root, "snapshots")
    version = next_version(snap_root)
    outdir = os.path.join(snap_root, "v%d" % version)
    os.makedirs(os.path.join(outdir, "pages"), exist_ok=True)

    base = norm_url(args.url if "://" in args.url else "https://" + args.url)
    print("基线 URL: %s" % base)

    # robots.txt
    robots_text, robots_status, _, _ = fetch(urllib.parse.urljoin(base, "/robots.txt"), timeout=15)
    if robots_text is not None and robots_status == 200:
        with open(os.path.join(outdir, "robots.txt"), "w", encoding="utf-8") as f:
            f.write(robots_text)

    # sitemap 原文
    sm_text, sm_status, _, _ = fetch(urllib.parse.urljoin(base, "/sitemap.xml"), timeout=15)
    if sm_text is not None and sm_status == 200:
        with open(os.path.join(outdir, "sitemap.xml"), "w", encoding="utf-8") as f:
            f.write(sm_text)

    # 待抓页面：首页 + 显式指定 + sitemap 抽样
    page_urls = [base]
    seen = {norm_url(base)}
    for pu in args.pages or []:
        full = pu if pu.startswith("http") else urllib.parse.urljoin(base, pu)
        n = norm_url(full)
        if n not in seen:
            seen.add(n)
            page_urls.append(full)
    if not args.no_sitemap:
        budget = args.max_pages - len(page_urls)
        if budget > 0:
            for u in sample_sitemap_pages(base, robots_text, budget):
                n = norm_url(u)
                if n not in seen:
                    seen.add(n)
                    page_urls.append(u)
    page_urls = page_urls[: args.max_pages]

    seo_doc = {
        "base_url": base,
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "pages": {},
    }
    meta_doc = {
        "version": version, "base_url": base, "tool": "sitediff.py " + TOOL_VERSION,
        "created_at": seo_doc["created_at"],
        "robots_status": robots_status, "sitemap_status": sm_status,
        "screenshot": not args.no_screenshot, "pages": [], "notes": [],
    }

    for u in page_urls:
        n = norm_url(u)
        slug = slug_for(n)
        print("抓取: %s" % u)
        html_text, status, final_url, ctype = fetch(u)
        entry = {"fetch_status": status, "final_url": final_url}
        if html_text is not None and status == 200 and is_html_content(ctype, html_text):
            with open(os.path.join(outdir, "pages", slug + ".html"), "w", encoding="utf-8") as f:
                f.write(html_text)
            entry["page_file"] = "pages/%s.html" % slug
            entry.update(extract_page_seo(n, html_text))
            print("  ✓ title=%r  H1×%d  内链%d  外链%d" % (
                (entry.get("title") or "")[:40], len(entry["headings"]["h1"]),
                len(entry["links"]["internal"]), len(entry["links"]["external"])))
        else:
            entry["error"] = "HTTP %s / 非HTML内容" % status
            meta_doc["notes"].append("%s 抓取异常: %s" % (n, status))
            print("  ✗ 抓取异常 (status=%s)" % status)

        if not args.no_screenshot:
            shot = os.path.join(outdir, "screenshots", slug + ".png")
            os.makedirs(os.path.dirname(shot), exist_ok=True)
            ok, err = take_screenshot(u, shot)
            if ok:
                entry["screenshot"] = "screenshots/%s.png" % slug
            else:
                meta_doc["notes"].append("%s 截图失败: %s" % (n, err))

        seo_doc["pages"][n] = entry
        meta_doc["pages"].append(n)

    with open(os.path.join(outdir, "seo.json"), "w", encoding="utf-8") as f:
        json.dump(seo_doc, f, ensure_ascii=False, indent=2, sort_keys=True)
    with open(os.path.join(outdir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta_doc, f, ensure_ascii=False, indent=2, sort_keys=True)

    print("\n快照已保存: %s (v%d, %d 个页面)" % (outdir, version, len(seo_doc["pages"])))

    if args.commit:
        subprocess.run(["git", "add", outdir], cwd=root, check=False)
        r = subprocess.run(
            ["git", "commit", "-m", "snapshot v%d of %s" % (version, base)],
            cwd=root, capture_output=True, text=True,
        )
        if r.returncode == 0:
            subprocess.run(["git", "tag", "v%d" % version], cwd=root, check=False)
            print("已提交 git 并打标签 v%d" % version)
        else:
            print("git 提交失败: %s" % (r.stderr or r.stdout).strip())
    else:
        print("提示: 加 --commit 可自动 git 提交并打标签 v%d" % version)


# ---------------------------------------------------------------------------
# compare 子命令
# ---------------------------------------------------------------------------

HIGH, MED, LOW, INFO = "high", "medium", "low", "info"
EMOJI = {HIGH: "🔴", MED: "🟡", LOW: "🟢", INFO: "ℹ️"}
LEVEL_CN = {HIGH: "高影响", MED: "中影响", LOW: "低影响", INFO: "信息"}
LEVEL_ORDER = {HIGH: 0, MED: 1, LOW: 2, INFO: 3}


def json_ld_key(obj):
    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)
    except Exception:
        return repr(obj)


def json_ld_types(objs):
    types = []
    for o in objs:
        if isinstance(o, dict):
            t = o.get("@type")
            if isinstance(t, list):
                types.extend(str(x) for x in t)
            elif t:
                types.append(str(t))
    return sorted(types)


def set_diff(old, new):
    old_s, new_s = set(old or []), set(new or [])
    return sorted(new_s - old_s), sorted(old_s - new_s)


def compare_page(url, a, b):
    """对比单页，返回 (issues, text_diff)。issue = (level, category, message)"""
    issues = []

    def add(level, cat, msg):
        issues.append((level, cat, msg))

    if a.get("error") and not b.get("error"):
        add(HIGH, "status", "旧版抓取失败（%s），新版正常，以下对比可能不完整" % a.get("fetch_status"))
    if not a.get("error") and b.get("error"):
        add(HIGH, "page", "页面不可用/抓取失败（HTTP %s）" % b.get("fetch_status"))
        return issues, ""
    if a.get("error") and b.get("error"):
        return issues, ""

    # title
    ta, tb = a.get("title"), b.get("title")
    if ta and not tb:
        add(HIGH, "title", "title 丢失: %r → 空" % ta)
    elif tb and not ta:
        add(LOW, "title", "新增 title: %r" % tb)
    elif ta != tb:
        ratio = difflib.SequenceMatcher(None, ta or "", tb or "").ratio()
        if ratio < 0.5:
            add(HIGH, "title", "title 大幅改动（相似度 %.0f%%）: %r → %r" % (ratio * 100, ta, tb))
        else:
            add(MED, "title", "title 变化: %r → %r" % (ta, tb))

    # meta description
    da, db = a.get("meta_description"), b.get("meta_description")
    if da and not db:
        add(MED, "description", "meta description 丢失")
    elif not da and db:
        add(LOW, "description", "新增 meta description（%d 字符）" % len(db))
    elif da != db:
        add(LOW, "description", "meta description 更新（%d → %d 字符）" % (len(da or ""), len(db or "")))

    # robots meta
    ra, rb = (a.get("meta_robots") or "").lower(), (b.get("meta_robots") or "").lower()
    if "noindex" in rb and "noindex" not in ra:
        add(HIGH, "robots", "新增 noindex（当前: %r）— 页面将退出搜索引擎索引" % b.get("meta_robots"))
    if "nofollow" in rb and "nofollow" not in ra:
        add(MED, "robots", "新增 nofollow")

    # canonical
    ca, cb = a.get("canonical"), b.get("canonical")
    if ca and not cb:
        add(HIGH, "canonical", "canonical 丢失（旧: %s）" % ca)
    elif cb and not ca:
        add(LOW, "canonical", "新增 canonical → %s" % cb)
    elif ca != cb:
        add(HIGH, "canonical", "canonical 变化: %s → %s" % (ca, cb))

    # html lang
    if a.get("html_lang") != b.get("html_lang"):
        add(MED, "html_lang", "<html lang> 变化: %r → %r" % (a.get("html_lang"), b.get("html_lang")))

    # hreflang
    ha = {(h["hreflang"], h["href"]) for h in a.get("hreflang") or []}
    hb = {(h["hreflang"], h["href"]) for h in b.get("hreflang") or []}
    if ha != hb:
        add(MED, "hreflang", "hreflang 集合变化: +%d / -%d" % (len(hb - ha), len(ha - hb)))

    # OG / Twitter
    for label, key in (("Open Graph", "open_graph"), ("Twitter Cards", "twitter_cards")):
        oa, ob = a.get(key) or {}, b.get(key) or {}
        if oa != ob:
            removed = sorted(set(oa) - set(ob))
            added = sorted(set(ob) - set(oa))
            changed = sorted(k for k in set(oa) & set(ob) if oa[k] != ob[k])
            if removed:
                add(MED, key, "%s 标签丢失: %s" % (label, ", ".join(removed)))
            if added:
                add(LOW, key, "%s 新增标签: %s" % (label, ", ".join(added)))
            if changed:
                add(LOW, key, "%s 值变化: %s" % (label, ", ".join(changed)))

    # JSON-LD
    ja, jb = a.get("json_ld") or [], b.get("json_ld") or []
    if ja and not jb:
        add(HIGH, "jsonld", "结构化数据（JSON-LD）全部丢失（旧类型: %s）" % (json_ld_types(ja) or "未知"))
    elif jb and not ja:
        add(LOW, "jsonld", "新增结构化数据: %s" % (json_ld_types(jb) or "-"))
    elif ja != jb:
        ka = sorted(json_ld_key(x) for x in ja)
        kb = sorted(json_ld_key(x) for x in jb)
        if ka != kb:
            add(MED, "jsonld", "结构化数据变化: 类型 %s → %s" % (json_ld_types(ja) or "-", json_ld_types(jb) or "-"))

    # H1
    h1a, h1b = a.get("headings", {}).get("h1") or [], b.get("headings", {}).get("h1") or []
    if h1a and not h1b:
        add(HIGH, "h1", "H1 丢失（旧: %r）" % h1a[0])
    elif len(h1b) > 1 and len(h1b) != len(h1a):
        add(MED, "h1", "出现 %d 个 H1（建议仅保留 1 个）" % len(h1b))
    elif h1a and h1b and h1a[0] != h1b[0]:
        add(LOW, "h1", "H1 文案变化: %r → %r" % (h1a[0], h1b[0]))

    # 标题结构
    for tag in ("h2", "h3"):
        va, vb = a.get("headings", {}).get(tag) or [], b.get("headings", {}).get(tag) or []
        if va != vb:
            add(LOW, "headings", "%s 结构变化: %d → %d 条" % (tag.upper(), len(va), len(vb)))

    # 图片 alt
    ia, ib = a.get("images") or [], b.get("images") or []
    cov_a, cov_b = a.get("images_alt_coverage"), b.get("images_alt_coverage")
    src_a = {i["src"] for i in ia}
    src_b = {i["src"] for i in ib}
    new_imgs = [i for i in ib if i["src"] not in src_a]
    if new_imgs:
        no_alt = [i for i in new_imgs if not (i["alt"] or "").strip()]
        add(LOW, "images", "新增图片 %d 张（其中 %d 张缺 alt）" % (len(new_imgs), len(no_alt)))
    if src_a - src_b:
        add(LOW, "images", "移除图片 %d 张" % len(src_a - src_b))
    if cov_a is not None and cov_b is not None and cov_b < cov_a - 0.05:
        add(MED, "images", "图片 alt 覆盖率下降: %.0f%% → %.0f%%" % (cov_a * 100, cov_b * 100))

    # 链接
    la, lb = a.get("links") or {}, b.get("links") or {}
    int_a, int_b = la.get("internal") or [], lb.get("internal") or []
    added_i, removed_i = set_diff(int_a, int_b)
    if removed_i:
        if int_a and len(removed_i) / max(len(int_a), 1) > 0.2:
            add(MED, "links", "内链明显减少: %d → %d（移除 %d 条）" % (len(int_a), len(int_b), len(removed_i)))
        else:
            add(LOW, "links", "内链变化: +%d / -%d" % (len(added_i), len(removed_i)))
    elif added_i:
        add(LOW, "links", "新增内链 %d 条" % len(added_i))
    ext_a, ext_b = la.get("external") or [], lb.get("external") or []
    added_e, removed_e = set_diff(ext_a, ext_b)
    if added_e or removed_e:
        add(LOW, "links", "外链变化: +%d / -%d" % (len(added_e), len(removed_e)))

    # 文本量与文本 diff
    wa, wb = a.get("word_count") or 0, b.get("word_count") or 0
    if wa and wb < wa * 0.7:
        add(MED, "content", "正文文字量大幅减少: %d → %d 词（-%.0f%%）" % (wa, wb, (1 - wb / wa) * 100))
    ta_lines, tb_lines = a.get("text_lines") or [], b.get("text_lines") or []
    text_diff = ""
    if ta_lines != tb_lines:
        add(LOW, "content", "页面文案有改动（约 +%d / -%d 行）" % (
            sum(1 for l in tb_lines if l not in ta_lines),
            sum(1 for l in ta_lines if l not in tb_lines)))
        diff = list(difflib.unified_diff(ta_lines, tb_lines, fromfile="旧版", tofile="新版", lineterm=""))
        if diff:
            text_diff = "\n".join(diff[:120])

    return issues, text_diff


RECOMMENDATIONS = {
    "noindex": "页面被标记 noindex，若非刻意下线请立即移除，否则将退出搜索引擎索引。",
    "canonical": "canonical 变化/丢失，请确认新指向是唯一权威版本，避免重复内容或收录错位。",
    "jsonld": "结构化数据丢失，建议恢复 JSON-LD 以保留富摘要（Rich Result）展示资格。",
    "h1": "H1 丢失或多重 H1，建议保留唯一且包含核心关键词的主标题。",
    "title": "title 为空或大幅改动，确认新版包含核心关键词并保持品牌格式。",
    "description": "缺失 meta description，建议补写约 120–160 字符的页面描述。",
    "open_graph": "OG/Twitter 标签丢失会导致社交分享预览退化，建议恢复。",
    "twitter_cards": "OG/Twitter 标签丢失会导致社交分享预览退化，建议恢复。",
    "images": "图片 alt 覆盖率下降，为新增图片补充描述性 alt 文本。",
    "links": "内链数量明显减少，检查导航/页脚/正文链接是否在改版中意外丢失。",
    "content": "正文内容大幅减少，确认关键内容没有在改版中丢失。",
    "page": "页面 404/不可用，若已有外链或收录，请设置 301 跳转到最相关替代页。",
    "robots": "检查 robots 设置是否为有意变更。",
}


def cmd_compare(args):
    root = os.path.dirname(os.path.abspath(__file__))
    snap_root = os.path.abspath(args.snapshots_dir) if args.snapshots_dir else os.path.join(root, "snapshots")
    if args.auto:
        vs = latest_versions(snap_root, 2)
        if len(vs) < 2:
            sys.exit("错误: 至少需要两个快照才能对比（当前 %d 个）" % len(vs))
        va_num, vb_num = vs[1], vs[0]
    else:
        try:
            va_num = int(str(args.version_a).lstrip("vV"))
            vb_num = int(str(args.version_b).lstrip("vV"))
        except (TypeError, ValueError):
            sys.exit("错误: 版本号格式应为 v1 / 1")
    da = os.path.join(snap_root, "v%d" % va_num)
    db = os.path.join(snap_root, "v%d" % vb_num)
    for d in (da, db):
        if not os.path.isfile(os.path.join(d, "seo.json")):
            sys.exit("错误: 找不到快照 %s" % d)

    with open(os.path.join(da, "seo.json"), encoding="utf-8") as f:
        old = json.load(f)
    with open(os.path.join(db, "seo.json"), encoding="utf-8") as f:
        new = json.load(f)

    va, vb = "v%d" % va_num, "v%d" % vb_num
    pages_a, pages_b = old.get("pages", {}), new.get("pages", {})
    all_pages = sorted(set(pages_a) | set(pages_b))

    all_issues = []     # (page, level, category, message)
    page_details = {}   # page -> {"seo": [...], "diff": str}

    for pg in all_pages:
        a, b = pages_a.get(pg), pages_b.get(pg)
        if a is None:
            all_issues.append((pg, INFO, "page", "新增页面"))
            page_details[pg] = {"seo": ["ℹ️ 新增页面（此版本新抓取）"], "diff": ""}
            continue
        if b is None:
            all_issues.append((pg, HIGH, "page", "页面在快照中消失（可能被移除或未抓取）"))
            page_details[pg] = {"seo": ["🔴 页面在 %s 中消失" % vb], "diff": ""}
            continue
        issues, text_diff = compare_page(pg, a, b)
        for lv, cat, msg in issues:
            all_issues.append((pg, lv, cat, msg))
        page_details[pg] = {
            "seo": ["%s %s" % (EMOJI[lv], msg) for lv, cat, msg in issues] or ["✅ 无变化"],
            "diff": text_diff,
        }

    counts = {HIGH: 0, MED: 0, LOW: 0, INFO: 0}
    for _, lv, _, _ in all_issues:
        counts[lv] += 1

    lines = []
    lines.append("# 网站版本对比报告：%s → %s" % (va, vb))
    lines.append("")
    lines.append("- 基线 URL: `%s`" % new.get("base_url", old.get("base_url", "?")))
    lines.append("- 对比范围: %s（%s）vs %s（%s），共 %d 个页面" % (
        va, old.get("created_at", "?"), vb, new.get("created_at", "?"), len(all_pages)))
    lines.append("")
    lines.append("## 变化总览")
    lines.append("")
    lines.append("| 级别 | 数量 |")
    lines.append("|---|---|")
    for lv in (HIGH, MED, LOW, INFO):
        lines.append("| %s %s | %d |" % (EMOJI[lv], LEVEL_CN[lv], counts[lv]))
    lines.append("")

    lines.append("## SEO 变化")
    lines.append("")
    for pg in all_pages:
        det = page_details.get(pg)
        if not det:
            continue
        lines.append("### %s" % pg)
        lines.append("")
        for s in det["seo"]:
            lines.append("- %s" % s)
        lines.append("")

    lines.append("## 网页内容变化")
    lines.append("")
    any_content = False
    for pg in all_pages:
        det = page_details.get(pg) or {}
        if det.get("diff"):
            any_content = True
            lines.append("### %s" % pg)
            lines.append("")
            lines.append("<details><summary>文本 diff（前 120 行）</summary>")
            lines.append("")
            lines.append("```diff")
            lines.append(det["diff"])
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")
    if not any_content:
        lines.append("✅ 页面可见文本无实质变化。")
        lines.append("")

    # 截图对比
    shot_pairs = []
    for pg in all_pages:
        a, b = pages_a.get(pg), pages_b.get(pg)
        if a and b and a.get("screenshot") and b.get("screenshot"):
            shot_pairs.append((pg, os.path.join(va, a["screenshot"]), os.path.join(vb, b["screenshot"])))
    if shot_pairs:
        lines.append("## 截图对比")
        lines.append("")
        for pg, sa, sb in shot_pairs:
            lines.append("### %s" % pg)
            lines.append("")
            lines.append("| 旧版 | 新版 |")
            lines.append("|---|---|")
            lines.append("| ![旧版](../snapshots/%s) | ![新版](../snapshots/%s) |" % (sa, sb))
            lines.append("")

    # 修复建议
    recs = []
    seen_cats = set()
    seen_recs = set()
    for _, lv, cat, _ in sorted(all_issues, key=lambda x: (LEVEL_ORDER.get(x[1], 9), x[2])):
        if (lv in (HIGH, MED) and cat in RECOMMENDATIONS and cat not in seen_cats
                and RECOMMENDATIONS[cat] not in seen_recs):
            seen_cats.add(cat)
            seen_recs.add(RECOMMENDATIONS[cat])
            recs.append(RECOMMENDATIONS[cat])
    if recs:
        lines.append("## 修复建议（按优先级）")
        lines.append("")
        for i, r in enumerate(recs, 1):
            lines.append("%d. %s" % (i, r))
        lines.append("")

    report = "\n".join(lines).rstrip() + "\n"
    out = os.path.abspath(args.output) if args.output else os.path.join(
        root, "reports", "%s-%s.md" % (va, vb))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(report)

    print("对比完成: %s → %s" % (va, vb))
    print("🔴 %d  🟡 %d  🟢 %d  ℹ️ %d" % (counts[HIGH], counts[MED], counts[LOW], counts[INFO]))
    print("报告已写入: %s" % out)


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="网站版本快照与 SEO 对比工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("snapshot", help="抓取网站并保存为新版本快照")
    sp.add_argument("url", help="网站 URL（首页）")
    sp.add_argument("--pages", nargs="*", help="额外指定页面路径或完整 URL")
    sp.add_argument("--max-pages", type=int, default=10, help="最多抓取页面数（默认 10）")
    sp.add_argument("--no-sitemap", action="store_true", help="不从 sitemap 抽样额外页面")
    sp.add_argument("--no-screenshot", action="store_true", help="跳过截图")
    sp.add_argument("--commit", action="store_true", help="快照后自动 git 提交并打标签")
    sp.add_argument("--output-dir", help="快照输出目录（默认 ./snapshots，测试用）")
    sp.set_defaults(func=cmd_snapshot)

    cp = sub.add_parser("compare", help="对比两个版本快照并生成报告")
    cp.add_argument("version_a", nargs="?", help="旧版本号，如 v1")
    cp.add_argument("version_b", nargs="?", help="新版本号，如 v2")
    cp.add_argument("--auto", action="store_true", help="自动对比最近两个版本")
    cp.add_argument("--output", help="报告输出路径（默认 reports/vA-vB.md）")
    cp.add_argument("--snapshots-dir", help="快照目录（默认 ./snapshots）")
    cp.set_defaults(func=cmd_compare)

    args = ap.parse_args()
    if args.cmd == "compare" and not args.auto and not (args.version_a and args.version_b):
        ap.error("compare 需要 vA vB 两个版本号，或使用 --auto")
    args.func(args)


if __name__ == "__main__":
    main()
