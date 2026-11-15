#!/usr/bin/env python3
"""把公开静态小游戏整站抓成可离线托管的镜像（GitHub Actions 内运行）。

用法：TARGET_URL=<带或不带 query 的入口地址> python3 .github/scripts/mirror_site.py

产物：site/ 目录（直接作为 gh-pages 根），以及 site/_mirror.json 清单。
策略：入口 URL 所在目录视为站点根；所有同源资源按 URL path 映射到 site/ 下的
相对路径，并改写 HTML/CSS/JS 里的引用；跨域资源保留原引用并记入 external。
"""
import base64
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

TARGET = os.environ.get("TARGET_URL", "").strip()
if not TARGET:
    print("!! TARGET_URL 未设置", file=sys.stderr)
    sys.exit(1)

OUT = "site"
MAX_FILES = 400
MAX_BYTES = 30 * 1024 * 1024
MAX_DEPTH = 4

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
    "Connection": "close",
}

parsed_target = urllib.parse.urlparse(TARGET)
BASE_NETLOC = parsed_target.netloc
BASE_DIR = posixpath.dirname(parsed_target.path) or "/"
if not BASE_DIR.endswith("/"):
    BASE_DIR += "/"

os.makedirs(OUT, exist_ok=True)
visited = set()
external = set()
warnings = []
files = {}
queue = deque()

ATTR_RE = re.compile(
    r"""\b(src|href|poster|data-src|data|action)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.I,
)
CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.I)
JS_IMPORT_RE = re.compile(
    r"""((?:\bfrom|\bimport\b\s*\(|\bimport|\brequire\s*\()\s*['"])([^'"]+)(['"])""",
    re.I,
)
JS_NEWURL_RE = re.compile(r"""new\s+URL\(\s*(['"])([^'"]+)\1""", re.I)
BASE_TAG_RE = re.compile(r"<base\b[^>]*>", re.I)


def norm_url(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))


def is_same_origin(url: str) -> bool:
    return urllib.parse.urlsplit(url).netloc == BASE_NETLOC


def local_path_for(url: str) -> str:
    """把同源 URL 映射到 site/ 下的相对路径。"""
    p = urllib.parse.urlsplit(url)
    path = urllib.parse.unquote(p.path) or "/"
    if path.startswith(BASE_DIR):
        rel = path[len(BASE_DIR):]
    else:
        rel = path.lstrip("/")
        warnings.append(f"资源位于入口目录之外，按根路径落盘: {url}")
    rel = rel.lstrip("/") or "index.html"
    rel = posixpath.normpath(rel)
    if rel.startswith("..") or rel == ".":
        rel = posixpath.basename(path) or "asset.bin"
        warnings.append(f"路径越界，改存为 {rel}: {url}")
    if rel.endswith("/"):
        rel += "index.html"
    return rel


def ref_for(target_local: str, current_local: str) -> str:
    start = posixpath.dirname(current_local)
    r = posixpath.relpath(target_local, start or ".")
    if not r.startswith("."):
        r = "./" + r
    return r


def enqueue(spec: str, current_url: str, current_local: str, depth: int, kind_hint=None):
    """返回 (改写后的 spec, 目标 local path)；无法本地化则返回 (原 spec, None)。"""
    spec = (spec or "").strip()
    if not spec or spec.startswith(("#", "data:", "blob:", "javascript:", "mailto:", "tel:")):
        return spec, None
    absolute = urllib.parse.urljoin(current_url, spec)
    if absolute.startswith("//"):
        absolute = parsed_target.scheme + ":" + absolute
    if not is_same_origin(absolute):
        external.add(absolute)
        return spec, None
    absolute = norm_url(absolute)
    target_local = local_path_for(absolute)
    if depth < MAX_DEPTH and absolute not in visited and len(files) + len(queue) < MAX_FILES:
        queue.append((absolute, target_local, depth + 1, kind_hint))
    return ref_for(target_local, current_local), target_local


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=35) as r:
                data = r.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise RuntimeError(f"文件超过 {MAX_BYTES} 字节: {url}")
                return data
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == 2:
                raise
            import time
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError("unreachable")


def kind_of(url: str, data: bytes) -> str:
    path = urllib.parse.urlsplit(url).path.lower()
    if path.endswith((".css",)) or b"<style" in data[:200] and data[:1] == b".":
        return "css"
    if path.endswith((".js", ".mjs", ".cjs", ".ts", ".jsx")):
        return "js"
    if path.endswith((".html", ".htm")) or not path or path.endswith("/"):
        return "html"
    if data[:200].lstrip().startswith((b"<!doctype", b"<html", b"<head", b"<body")):
        return "html"
    if path.endswith((".json", ".map", ".webmanifest", ".svg", ".xml", ".txt")):
        return "text"
    return "binary"


def rewrite_html(data: bytes, url: str, local: str, depth: int) -> bytes:
    text = data.decode("utf-8", "replace")
    # <base href> 会改变相对路径基准，静态镜像里移除
    if BASE_TAG_RE.search(text):
        warnings.append(f"已移除 <base> 标签: {url}")
        text = BASE_TAG_RE.sub("", text)

    def sub_attr(m):
        raw = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else m.group(4))
        quote = '"' if m.group(2) is not None else ("'" if m.group(3) is not None else "")
        if m.group(1).lower() == "srcset" or ("," in raw and " " in raw):
            parts = []
            for cand in raw.split(","):
                c = cand.strip()
                if not c:
                    continue
                token = c.split()[0]
                rest = c[len(token):]
                new, _ = enqueue(token, url, local, depth)
                parts.append(new + rest)
            return f'{m.group(1)}={quote}{", ".join(parts)}{quote}'
        new, _ = enqueue(raw, url, local, depth)
        return f"{m.group(1)}={quote}{new}{quote}"

    text = ATTR_RE.sub(sub_attr, text)
    # 内联 <script> / <style> 里的相对引用也扫一遍
    text = rewrite_css_urls(text, url, local, depth)
    return text.encode("utf-8")


def rewrite_css_urls(text: str, url: str, local: str, depth: int) -> str:
    def sub(m):
        q, raw = m.group(1), m.group(2)
        new, _ = enqueue(raw, url, local, depth)
        return f"url({q}{new}{q})"
    return CSS_URL_RE.sub(sub, text)


def rewrite_js(data: bytes, url: str, local: str, depth: int) -> bytes:
    text = data.decode("utf-8", "replace")

    def sub_import(m):
        prefix, raw, q = m.group(1), m.group(2), m.group(3)
        new, _ = enqueue(raw, url, local, depth)
        return prefix + new + q

    def sub_newurl(m):
        q, raw = m.group(1), m.group(2)
        new, _ = enqueue(raw, url, local, depth)
        return f"new URL({q}{new}{q}"

    text = JS_IMPORT_RE.sub(sub_import, text)
    text = JS_NEWURL_RE.sub(sub_newurl, text)
    return text.encode("utf-8")


def save(local: str, data: bytes, url: str, kind: str):
    path = os.path.join(OUT, *local.split("/"))
    os.makedirs(os.path.dirname(path) or OUT, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    files[local] = {
        "source": url,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "kind": kind,
    }
    print(f"  + {local:<64} {len(data):>9} B  {kind}")


# ---- 入口 ----
entry = norm_url(TARGET)
entry_local = "index.html"
print(f"TARGET   = {entry}")
print(f"BASE_DIR = {BASE_DIR}")
print(f"ORIGIN   = {BASE_NETLOC}")
try:
    entry_data = fetch(entry)
except Exception as e:
    print(f"!! 入口抓取失败: {e}", file=sys.stderr)
    sys.exit(2)
print(f"入口 {len(entry_data)} B · {kind_of(entry, entry_data)}")
visited.add(entry)
queue.append((entry, entry_local, 0, "html"))

while queue:
    url, local, depth, hint = queue.popleft()
    key = norm_url(url)
    if key in visited and local != entry_local:
        continue
    visited.add(key)
    try:
        data = fetch(url)
    except Exception as e:
        warnings.append(f"抓取失败 {url}: {e}")
        print(f"  ! FAIL {url} :: {e}")
        continue
    kind = hint or kind_of(url, data)
    if kind == "html":
        data = rewrite_html(data, url, local, depth)
    elif kind == "css":
        data = rewrite_css_urls(data.decode("utf-8", "replace"), url, local, depth).encode("utf-8")
    elif kind == "js":
        data = rewrite_js(data, url, local, depth)
    elif kind == "text":
        # json / svg 里出现的相对资源也尝试抓
        s = data.decode("utf-8", "replace")
        s2 = rewrite_css_urls(s, url, local, depth)
        data = s2.encode("utf-8")
    save(local, data, url, kind)
    if len(files) >= MAX_FILES:
        warnings.append(f"达到文件上限 {MAX_FILES}，停止递归")
        break

manifest = {
    "target": TARGET,
    "entry": entry,
    "base_dir": BASE_DIR,
    "file_count": len(files),
    "total_bytes": sum(v["bytes"] for v in files.values()),
    "files": files,
    "external": sorted(external),
    "warnings": sorted(set(warnings)),
}
manifest_path = os.path.join(OUT, "_mirror.json")
with open(manifest_path, "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)
print(f"\n完成：{len(files)} 文件 / {manifest['total_bytes']} B")
print(f"外链 {len(external)} 条，告警 {len(set(warnings))} 条")
for e in sorted(external)[:20]:
    print("  ~ external:", e)
for w in sorted(set(warnings))[:20]:
    print("  ~ warn:", w)
if not files:
    sys.exit(3)
