#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小可运行示例：只读拉取邮件 + MIME 解析 + 链接型超大附件下载。

只依赖 Python 标准库，不含任何分类/归档规则。所有账号信息从环境变量读取：

    MAIL_ADDRESS      邮箱地址（示例：you@example.com）
    MAIL_AUTH_CODE    客户端授权码（不是登录密码）
    MAIL_IMAP_HOST    IMAP 主机（默认按邮箱域名推断 imap.<domain>）
    MAIL_IMAP_PORT    默认 993
    MAIL_POP_HOST     POP3 主机（默认按邮箱域名推断 pop.<domain>）
    MAIL_POP_PORT     默认 995

用法：

    # 离线自测：解析一个 .eml，不联网、不需要账号
    python minimal_fetch.py --eml sample.eml --out ./out

    # 只读拉取最近 10 封（IMAP）
    python minimal_fetch.py --proto imap --limit 10 --out ./out

    # POP3 降级 + 真正下载超大附件（默认只报告链接，不联网下载）
    python minimal_fetch.py --proto pop3 --limit 10 --out ./out --download-big

说明：QQ / 网易的超大附件接口都是非公开 Web 接口，随时可能变化；
本示例默认只识别链接、不发起任何外部请求，离线自测因此不需要账号也不会触网。
加 --download-big 才会真正解析接口并下载文件。
"""

import argparse
import email
import hashlib
import html as html_mod
import http.cookiejar
import imaplib
import json
import os
import poplib
import re
import sys
import urllib.parse
import urllib.request
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
QQ_FTN_API = "https://wx.mail.qq.com/ftn/download"
QQ_REFERER = "https://wx.mail.qq.com/"
NETEASE_SUFFIXES = ("163.com", "126.com", "yeah.net", "188.com")
NETEASE_KEYWORDS = ("文件中转站", "网易网盘", "云附件", "超大附件")
ID_ARGS = '("name" "minimal-fetch" "version" "1.0" "vendor" "local")'

JS_X26_RE = re.compile(r"\\x26|\\u0026", re.I)
URL_RE = re.compile(r'https?://[^\s"\'<>()\\\u3000]+', re.I)


# ---------------------------------------------------------------- 通用工具

def decoded(raw, default=""):
    """RFC 2047 解码（主题 / 发件人 / 附件名）。"""
    if not raw:
        return default
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def safe_name(name, fallback="file", maxlen=60):
    """清洗文件名：替换非法字符、保留扩展名。"""
    name = (name or "").strip()
    root, ext = os.path.splitext(name)
    root = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", root)
    root = re.sub(r"\s+", " ", root).strip(" .")
    if not root:
        root = fallback
    root = root[:maxlen].rstrip()
    return root + ext


def unique_path(path):
    """目标已存在时加 (2)、(3)，绝不覆盖。"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists("%s(%d)%s" % (base, n, ext)):
        n += 1
    return "%s(%d)%s" % (base, n, ext)


def alt_key(parsed):
    """备用指纹：规范化时间 + 主题。跨协议切换时靠它接上历史记录。"""
    return "%s|%s" % (parsed.get("date", ""), parsed.get("subject", ""))


# ---------------------------------------------------------------- 报文解析

def part_text(part):
    ctype = part.get_content_type()
    payload = part.get_payload(decode=True)
    if payload is None:
        return "", False
    charset = part.get_content_charset() or "utf-8"
    for enc in (charset, "utf-8", "gb18030", "gbk", "latin-1"):
        try:
            return payload.decode(enc, errors="strict"), ctype == "text/html"
        except Exception:
            continue
    return payload.decode("utf-8", errors="replace"), ctype == "text/html"


def parse_message(raw_bytes):
    """把一封原始报文解析成 subject/sender/date/plain/html/attachments。"""
    msg = email.message_from_bytes(raw_bytes)
    date_str = msg.get("Date") or ""
    try:
        date_str = parsedate_to_datetime(date_str).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass

    plain_parts, html_parts, attachments = [], [], []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            fname = part.get_filename()
            disp = (part.get("Content-Disposition") or "").lower()
            if fname or "attachment" in disp:
                data = part.get_payload(decode=True)
                if data is not None:
                    attachments.append({
                        "filename": decoded(fname, "") or "unnamed_%d" % (len(attachments) + 1),
                        "content_type": part.get_content_type(),
                        "size": len(data),
                        "data": data,
                    })
                continue
            text, is_html = part_text(part)
            if text:
                (html_parts if is_html else plain_parts).append(text)
    else:
        text, is_html = part_text(msg)
        if text:
            (html_parts if is_html else plain_parts).append(text)

    plain = "\n\n".join(plain_parts).strip()
    body_html = "\n\n".join(html_parts).strip()
    if not plain and body_html:
        txt = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", body_html)
        txt = re.sub(r"(?i)<br\s*/?>", "\n", txt)
        txt = re.sub(r"(?i)</(p|div|tr|h[1-6]|li)>", "\n", txt)
        txt = html_mod.unescape(re.sub(r"(?s)<[^>]+>", "", txt))
        plain = re.sub(r"\n{3,}", "\n\n", txt).strip()

    return {
        "subject": decoded(msg.get("Subject"), "(no subject)"),
        "sender": decoded(msg.get("From"), ""),
        "date": date_str,
        "plain": plain,
        "html": body_html,
        "attachments": attachments,
    }


# ---------------------------------------------------------------- 链接型超大附件

def iter_urls(text):
    """从 HTML 里尽力收集候选链接：href、JS 里的 url/downloadlink、裸文本 URL。"""
    out, seen = [], set()

    def push(raw):
        if not raw:
            return
        u = JS_X26_RE.sub("&", html_mod.unescape(raw)).strip().strip("\"'").strip()
        if not u.lower().startswith(("http://", "https://")):
            return
        u = u.rstrip(".,;:、。）】》\"'")
        if not u or u in seen:
            return
        if any(x.startswith(u) and len(x) > len(u) for x in seen):
            return
        seen.add(u)
        out.append(u)

    for m in re.finditer(r'href\s*=\s*["\']([^"\']+)["\']', text, re.I):
        push(m.group(1))
    for m in re.finditer(
            r'(?:var\s+url|downloadlink|download_url|downloadUrl|url)\s*[:=]\s*'
            r'["\']([^"\']+)["\']', text, re.I):
        push(m.group(1))
    for m in URL_RE.finditer(text):
        push(m.group(0))
    return out


def is_netease_host(host):
    host = (host or "").lower()
    return any(host == s or host.endswith("." + s) for s in NETEASE_SUFFIXES)


def resolve_qq(text, opener, log):
    """QQ：优先 QQMailBigAttach 块换直链，其次 GET 裸取件页解析。
    返回 [(文件名, 直链, 指纹)]。"""
    items, done = [], set()

    if "QQMailBigAttach" in text:
        blocks = re.findall(r'class="bigatt_bt"[^>]*>(.*?)(?=class="bigatt_bt"|$)',
                            text, re.S) or [text]
        for block in blocks:
            m = re.search(r'downloadlink="([^"]+)"', block)
            if not m:
                continue
            q = dict(urllib.parse.parse_qsl(
                urllib.parse.urlparse(html_mod.unescape(m.group(1))).query))
            key, code = q.get("key"), q.get("code")
            if not key or not code:
                continue
            done.add((key, code))
            mn = (re.search(r'<a style="color:#000;"[^>]*>([^<]+)</a>', block)
                  or re.search(r'title="([^"]*)"', block))
            name = mn.group(1).split("\n")[0].strip() if mn else ""
            try:
                data = urllib.parse.urlencode(
                    {"func": 3, "key": key, "code": code, "f": "json"}).encode()
                req = urllib.request.Request(
                    QQ_FTN_API, data=data,
                    headers={"User-Agent": UA, "Referer": QQ_REFERER,
                             "Content-Type": "application/x-www-form-urlencoded"})
                info = json.loads(opener.open(req, timeout=30).read().decode("utf-8", "ignore"))
                real = (info.get("body") or {}).get("url") or (info.get("data") or {}).get("url")
                if not real:
                    log("  [QQ] 接口未返回下载地址，跳过")
                    continue
                items.append((name or "qq_big_attach_%d" % (len(items) + 1),
                              JS_X26_RE.sub("&", real), "qq:%s:%s" % (key, code)))
            except Exception as e:
                log("  [QQ] 换直链失败：%s" % e)

    for url in iter_urls(text):
        low = url.lower()
        if not (low.startswith("https://wx.mail.qq.com/ftn/download")
                or "mail.qq.com/cgi-bin/ftnexs_download" in low):
            continue
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        if (q.get("key"), q.get("code")) in done:
            continue
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Referer": QQ_REFERER,
                              "Accept": "application/json, text/plain, */*"})
            resp = opener.open(req, timeout=30)
            raw = resp.read().decode("utf-8", "ignore")
            ctype = (resp.getheader("Content-Type") or "").lower()
        except Exception as e:
            log("  [QQ] 取件页打不开：%s" % e)
            continue
        name, real = "", ""
        if "json" in ctype or raw.lstrip().startswith("{"):
            try:
                body = (json.loads(raw).get("body") or {})
            except Exception:
                body = {}
            real = next((body[k] for k in ("url", "download_url", "fileUrl",
                                           "file_url", "downloadUrl", "link")
                         if body.get(k)), "")
            name = body.get("filename") or body.get("fileName") or ""
        else:
            m = re.search(r'var\s+url\s*=\s*"([^"]+)"', raw)
            real = m.group(1) if m else ""
        if not real:
            continue
        key, code = q.get("key") or "", q.get("code") or ""
        fp = ("qq:%s:%s" % (key, code)) if (key or code) else \
             ("qq:url:%s" % hashlib.sha1(url.encode()).hexdigest()[:8])
        items.append((name or "qq_big_attach_%d" % (len(items) + 1),
                      JS_X26_RE.sub("&", real), fp))
    return items


def resolve_netease(text, opener, log):
    """网易：取 linkKey → info/get 取文件名（可失败）→ dl/prepare 换直链。"""
    items, seen = [], set()
    for url in iter_urls(text):
        u = urllib.parse.urlparse(url)
        host = (u.netloc or "").lower()
        if u.scheme not in ("http", "https") or not is_netease_host(host):
            continue
        qs = urllib.parse.parse_qs(u.query)
        link_key = (qs.get("file") or qs.get("key") or [""])[0]
        if not link_key or (host, link_key) in seen:
            continue
        seen.add((host, link_key))

        filename = ""
        try:
            info_url = "%s://%s/filehub/bg/link/info/get?key=%s" % (
                u.scheme, host, urllib.parse.quote(link_key, safe=""))
            req = urllib.request.Request(info_url, headers={"User-Agent": UA})
            data = json.loads(opener.open(req, timeout=30).read().decode("utf-8", "ignore"))
            if data.get("code") == 200:
                filename = ((((data.get("data") or {}).get("linkInfo") or {})
                             .get("fileInfo") or {}).get("filename") or "").strip()
        except Exception as e:
            log("  [网易] 取文件名失败（不致命）：%s" % e)

        try:
            prep = "%s://%s/filehub/bg/dl/prepare" % (u.scheme, host)
            req = urllib.request.Request(
                prep, data=json.dumps({"linkKey": link_key}).encode("utf-8"),
                headers={"User-Agent": UA, "Content-Type": "application/json"})
            info = json.loads(opener.open(req, timeout=30).read().decode("utf-8", "ignore"))
            real = (info.get("data") or {}).get("downloadUrl") or info.get("downloadUrl")
            if not real:
                code, desc = info.get("code", -1), (info.get("desc") or "").upper()
                hint = "已过期" if code == 601 or desc == "EXPIRED" else "链接失效或无下载地址"
                log("  [网易] %s，跳过" % hint)
                continue
            items.append((filename or "netease_big_attach_%d" % (len(items) + 1),
                          real, "netease:%s:%s" % (host, link_key)))
        except Exception as e:
            log("  [网易] 换直链失败：%s" % e)
    return items


def disposition_name(headers, url):
    """从 Content-Disposition 补真实文件名：filename* → filename → URL 末段。"""
    cd = headers.get("Content-Disposition") or ""
    m = (re.search(r"filename\*\s*=\s*(?:utf-8|UTF-8)?'*\"?([^\";]+)", cd)
         or re.search(r"filename\s*=\s*\"?([^\";]+)\"?", cd))
    if m:
        return urllib.parse.unquote(m.group(1).strip())
    base = os.path.basename(urllib.parse.urlparse(url or "").path or "")
    return urllib.parse.unquote(base) if base else ""


def stream_download(url, adir, name, opener):
    """流式下载；占位名按响应头纠正；失败或空文件都清理干净。"""
    dest = unique_path(os.path.join(adir, safe_name(name)))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": url})
        resp = opener.open(req, timeout=120)
        got = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
        if got == 0:
            os.remove(dest)
            return None
        if "big_attach_" in name:
            real = disposition_name(resp.headers, url)
            if real:
                new = unique_path(os.path.join(adir, safe_name(real)))
                os.replace(dest, new)
                dest = new
        return dest
    except Exception as e:
        print("  附件下载失败（%s）：%s" % (e, name))
        if os.path.exists(dest):
            os.remove(dest)
        return None


def count_candidates(text):
    """只识别「像超大附件」的链接，不发起任何外部请求。"""
    n = 0
    for url in iter_urls(text):
        low = url.lower()
        if low.startswith("https://wx.mail.qq.com/ftn/download") \
                or "mail.qq.com/cgi-bin/ftnexs_download" in low:
            n += 1
            continue
        u = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(u.query)
        if is_netease_host(u.netloc) and (qs.get("file") or qs.get("key")):
            n += 1
    return n


def fetch_big(text, adir, log, do_download):
    """统一主干：识别/解析两家 → 去重指纹 → 下载。返回 (落盘路径列表, 候选链接数)。

    do_download=False 时只统计候选链接，完全不联网（离线自测用）。
    """
    if not text:
        return [], 0
    if not do_download:
        return [], count_candidates(text)
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        items = resolve_qq(text, opener, log) + resolve_netease(text, opener, log)
    except Exception as e:
        log("  超大附件解析异常（跳过）：%s" % e)
        return [], 0
    candidates = len(items)
    if not do_download:
        return [], candidates
    os.makedirs(adir, exist_ok=True)
    saved, used = [], set()
    for name, url, fp in items:
        if fp in used:
            continue
        used.add(fp)
        p = stream_download(url, adir, name, opener)
        if p:
            saved.append(p)
    return saved, candidates


# ---------------------------------------------------------------- 导出

def save_message(parsed, idx, out_root, seen=None, do_download=False):
    """把一封邮件写进独立目录：正文.txt / 正文.html / 附件/。"""
    dirname = "%04d_%s" % (idx, safe_name(parsed["subject"], "mail"))
    edir = os.path.join(out_root, dirname)
    os.makedirs(edir, exist_ok=True)

    header = ["# %s" % parsed["subject"], "",
              "- 发件人：%s" % parsed["sender"],
              "- 时间：%s" % (parsed["date"] or "未知"),
              "- 状态：%s" % ("未知" if seen is None else ("已读" if seen else "未读")),
              ""]
    atts = parsed["attachments"]
    header.append("- 常规附件（%d 个）：" % len(atts))
    header += ["  - %s（%.1f KB）" % (a["filename"], a["size"] / 1024) for a in atts]
    header += ["", "---", ""]
    body_path = os.path.join(edir, "正文.txt")
    with open(body_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(header))
        f.write(parsed["plain"] or "（无可提取的文本正文）")

    # 原始 HTML 必须保留：链接型附件的下载地址只在 HTML 里
    big_src = parsed["html"] or parsed["plain"]
    if parsed["html"]:
        with open(os.path.join(edir, "正文.html"), "w", encoding="utf-8", newline="\n") as f:
            f.write(parsed["html"])

    adir = os.path.join(edir, "附件")
    if atts:
        os.makedirs(adir, exist_ok=True)
        for a in atts:
            with open(unique_path(os.path.join(adir, safe_name(a["filename"]))), "wb") as f:
                f.write(a["data"])

    saved, candidates = fetch_big(big_src, adir, print, do_download)
    if not saved and (candidates or any(k in (big_src or "") for k in NETEASE_KEYWORDS)):
        with open(os.path.join(edir, "超大附件未拉取.txt"), "w", encoding="utf-8") as f:
            f.write("检测到链接型超大附件，但本次没有取回文件。\n")
            f.write("可能原因：未开启 --download-big、链接已过期、接口变化或当前无网络。\n")
    print("  已保存：%s（常规附件 %d，链接型附件 %d）"
          % (dirname, len(atts), len(saved)))
    return dirname


# ---------------------------------------------------------------- 协议接入

def load_state(out_root):
    """双指纹水位：主指纹与备用指纹分开，绝不合并。"""
    p = os.path.join(out_root, "_state.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return {"uid": set(d.get("uid") or []), "alt": set(d.get("alt") or [])}
    except Exception:
        return {"uid": set(), "alt": set()}


def save_state(out_root, state):
    p = os.path.join(out_root, "_state.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"uid": sorted(state["uid"]), "alt": sorted(state["alt"])},
                  f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def account():
    addr = os.environ.get("MAIL_ADDRESS", "").strip()
    code = os.environ.get("MAIL_AUTH_CODE", "").strip()
    if not addr or not code:
        raise SystemExit("请先设置环境变量 MAIL_ADDRESS 与 MAIL_AUTH_CODE")
    domain = addr.split("@")[-1]
    return {
        "address": addr, "code": code,
        "imap_host": os.environ.get("MAIL_IMAP_HOST") or ("imap." + domain),
        "imap_port": int(os.environ.get("MAIL_IMAP_PORT") or 993),
        "pop_host": os.environ.get("MAIL_POP_HOST") or ("pop." + domain),
        "pop_port": int(os.environ.get("MAIL_POP_PORT") or 995),
    }


def imap_fetch(cfg, state, limit=None, ignore_dedup=False):
    """IMAP 只读拉取：ID → EXAMINE → UID FLAGS → 只为新邮件 BODY.PEEK[]。"""
    if "ID" not in imaplib.Commands:
        imaplib.Commands["ID"] = ("NONAUTH", "AUTH", "SELECTED")
    imaplib._MAXLINE = 10_000_000
    m = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], timeout=60)
    try:
        m.login(cfg["address"], cfg["code"])
        try:
            typ, dat = m._simple_command("ID", ID_ARGS)
            m._untagged_response(typ, dat, "ID")
        except Exception as e:
            print("  服务器未接受 ID 命令（%s），继续" % e)
        typ, dat = m.select("INBOX", readonly=True)
        if typ != "OK":
            raise RuntimeError("打开收件箱失败：%s（可改用 --proto pop3）" % dat)
        print("  收件箱共 %s 封（只读方式打开）" % (dat[0] or b"0").decode())

        typ, rows = m.uid("FETCH", "1:*", "(UID FLAGS)")
        items = []
        for row in (rows or []):
            blob = row[0] if isinstance(row, tuple) else row
            if not isinstance(blob, (bytes, bytearray)):
                continue
            mu = re.search(r"UID (\d+)", blob.decode("utf-8", "replace"))
            if mu:
                items.append((int(mu.group(1)), "\\Seen" in blob.decode("utf-8", "replace")))
        items.sort()
        todo = [(u, s) for u, s in items
                if ignore_dedup or ("imap:%d" % u) not in state["uid"]]
        if limit:
            todo = todo[-limit:]
        print("  本次待取 %d 封" % len(todo))
        for uid, seen in todo:
            typ, rows = m.uid("FETCH", str(uid), "(FLAGS BODY.PEEK[])")
            raw = None
            for row in (rows or []):
                if isinstance(row, tuple):
                    raw = row[1]
            if raw:
                yield "imap:%d" % uid, raw, seen
    finally:
        try:
            m.logout()
        except Exception:
            pass


def pop_fetch(cfg, state, limit=None, ignore_dedup=False):
    """POP3 降级：UIDL → TOP 0 取头做备用指纹 → 命中才 RETR 全文。"""
    poplib._MAXLINE = 10_000_000
    p = poplib.POP3_SSL(cfg["pop_host"], cfg["pop_port"], timeout=60)
    try:
        p.user(cfg["address"])
        p.pass_(cfg["code"])
        _, lines, _ = p.uidl()
        pairs = []
        for line in lines:
            parts = line.decode("utf-8", "ignore").split()
            if len(parts) == 2:
                pairs.append((int(parts[0]), parts[1].strip().lower()))
        pairs.sort()
        cand = pairs[-limit:] if limit else pairs
        print("  收件箱共 %d 封，本次扫描 %d 封" % (len(pairs), len(cand)))
        for num, uidl in cand:
            if not ignore_dedup and uidl in state["uid"]:
                continue
            try:
                _, head_lines, _ = p.top(num, 0)
                parsed = parse_message(b"\n".join(head_lines) + b"\n\n")
                if not ignore_dedup and alt_key(parsed) in state["alt"]:
                    continue
                _, body_lines, _ = p.retr(num)
                yield uidl, b"\n".join(body_lines), None
            except Exception as e:
                print("  跳过第 %d 封：%s" % (num, e))
    finally:
        try:
            p.quit()
        except Exception:
            pass


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser(description="只读拉取邮件并保存正文与附件")
    ap.add_argument("--eml", help="离线解析一个 .eml 文件（不联网、不需要账号）")
    ap.add_argument("--proto", choices=("imap", "pop3"), default="imap")
    ap.add_argument("--limit", type=int, default=None, help="只处理最新 N 封候选")
    ap.add_argument("--all", action="store_true", help="忽略去重，全部重拉")
    ap.add_argument("--out", default="./out", help="输出目录")
    ap.add_argument("--download-big", action="store_true",
                    help="真正联网下载 QQ / 网易链接型超大附件（默认只报告）")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    if a.eml:
        with open(a.eml, "rb") as f:
            parsed = parse_message(f.read())
        save_message(parsed, 1, a.out, seen=None, do_download=a.download_big)
        return 0

    cfg = account()
    state = load_state(a.out)
    fetch = imap_fetch if a.proto == "imap" else pop_fetch
    n = 0
    for key, raw, seen in fetch(cfg, state, a.limit, a.all):
        parsed = parse_message(raw)
        n += 1
        save_message(parsed, n, a.out, seen=seen, do_download=a.download_big)
        state["uid"].add(key)
        state["alt"].add(alt_key(parsed))
        save_state(a.out, state)          # 每封都落盘，中断可续
    print("完成，本次处理 %d 封。" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
