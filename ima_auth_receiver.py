#!/usr/bin/env python3
"""
Local IMA auth receiver.

Runs on 127.0.0.1 only. A bookmarklet on ima.qq.com can POST captured
x-ima-bkn and x-ima-cookie headers here, then this receiver updates .env and
restarts the local IMA MCP LaunchAgent.
"""

from __future__ import annotations

import json
import html
import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from update_cookie import ENV_FILE, parse_headers_block, update_env


HOST = "127.0.0.1"
PORT = 8787
LABEL = "com.pp.ima-copilot-mcp"
PROJECT_DIR = Path(__file__).parent
RUNTIME_DIR = Path(os.environ.get("IMA_RUNTIME_DIR", Path.home() / ".claude/ima")).expanduser()
LOG_FILE = RUNTIME_DIR / "logs" / "ima_auth_receiver.log"
FIXED_KNOWLEDGE_BASE_ID = "7321325798449611"


BOOKMARKLET = r"""javascript:(()=>{alert('御札已发动。少女祈祷中：接下来我会监听 IMA 请求；现在在目标知识库里问一句。');const endpoint='http://127.0.0.1:8787/ima-auth';const norm=h=>{const out={};if(!h)return out;if(typeof Headers!=='undefined'&&h instanceof Headers){h.forEach((v,k)=>out[k.toLowerCase()]=v);return out}if(Array.isArray(h)){for(const [k,v]of h)out[String(k).toLowerCase()]=String(v);return out}for(const k of Object.keys(h))out[k.toLowerCase()]=String(h[k]);return out};const pickKb=x=>{try{if(!x)return null;if(typeof x==='string'){let m=x.match(/knowledgeBaseId%22%3A%22(\d{8,})|knowledgeBaseId[\"'=:%]+(\d{8,})|relatedUrl[\"'=:%]+(\d{8,})|(?:knowledge_base_id|knowledgeBaseId)[^0-9]{0,20}(\d{8,})|(?:kb|wiki|wikis|knowledge)[^0-9]{0,20}(\d{8,})/i);return m&&(m[1]||m[2]||m[3]||m[4]||m[5])}if(typeof x==='object'){for(const [k,v]of Object.entries(x)){if(/knowledge.?base.?id|relatedurl/i.test(k)&&String(v).match(/^\d{8,}$/))return String(v);const r=pickKb(v);if(r)return r}}}return null}catch(e){return null}};let sent=false,last={};const push=async(h,body,url)=>{const m=norm(h);last={...last,...m};const bkn=last['x-ima-bkn'];const cookie=last['x-ima-cookie'];const kb=pickKb(body)||pickKb(url)||pickKb(location.href)||last.kb;if(kb)last.kb=kb;if(!bkn||!cookie)return;if(sent&&(!kb||kb===last.sentKb))return;sent=true;last.sentKb=kb;try{const r=await fetch(endpoint,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({bkn,cookie,knowledgeBaseId:kb||null,url:String(url||location.href),body:typeof body==='string'?body.slice(0,5000):body})});const t=await r.text();alert(r.ok?'IMA 登录态/知识库御札已更新，扣子可以用了。':'御札更新失败：'+t.slice(0,300))}catch(e){sent=false;alert('没有连上本机接收结界：'+e.message)}};if(window.__codexImaHooked){alert('扣子已经在监听了。现在在 IMA 知识库里随便问一句就行。');return}window.__codexImaHooked=true;const hit=u=>String(u||'').includes('/cgi-bin/assistant/qa')||String(u||'').includes('/cgi-bin/session_logic/init_session');const oldFetch=window.fetch;window.fetch=function(input,init){try{const url=String((input&&input.url)||input||'');if(hit(url))push((init&&init.headers)||(input&&input.headers),(init&&init.body)||(input&&input.body),url)}catch(e){}return oldFetch.apply(this,arguments)};const proto=XMLHttpRequest.prototype;const oldOpen=proto.open;const oldSet=proto.setRequestHeader;const oldSend=proto.send;proto.open=function(method,url){this.__codexImaUrl=String(url||'');this.__codexImaHeaders={};return oldOpen.apply(this,arguments)};proto.setRequestHeader=function(k,v){try{this.__codexImaHeaders[String(k).toLowerCase()]=String(v)}catch(e){}return oldSet.apply(this,arguments)};proto.send=function(body){try{if(hit(this.__codexImaUrl))push(this.__codexImaHeaders,body,this.__codexImaUrl)}catch(e){}return oldSend.apply(this,arguments)}})()"""
BOOKMARKLET_HREF = html.escape(BOOKMARKLET, quote=True)


INSTALL_HTML = f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>扣子 IMA 登录态御札</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 40px; max-width: 760px; line-height: 1.6; }}
a.bookmarklet {{ display: inline-flex; align-items: center; justify-content: center; min-height: 44px; padding: 0 18px; border: 1px solid #111; border-radius: 6px; background: #111; color: #fff; text-decoration: none; font-weight: 700; cursor: grab; }}
button {{ min-height: 40px; padding: 0 14px; border: 1px solid #777; border-radius: 6px; background: #fff; color: #111; font-weight: 600; cursor: pointer; }}
.row {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
.hint {{ color: #555; }}
.step {{ margin: 18px 0; padding: 14px 16px; border-left: 4px solid #111; background: #f6f6f6; }}
code {{ background: #f4f4f4; padding: 2px 5px; border-radius: 4px; }}
</style>
<h1>扣子 IMA 登录态御札</h1>
<div class="step">
  <b>先测试：</b>
  <button type="button" id="test">试放一张小御札</button>
  <span class="hint">这里能弹窗，说明浏览器没拦提示。</span>
</div>
<p>下面这块黑色的是“书签御札”。把它拖到浏览器书签栏；之后去 IMA 页面点书签栏里的它。</p>
<p class="row">
  <a class="bookmarklet" href="{BOOKMARKLET_HREF}" title="拖到书签栏">更新 IMA 登录态御札</a>
  <button type="button" id="copy">复制御札代码</button>
</p>
<p class="hint">如果拖不动，就点“复制御札代码”，手动新建一个书签，把网址粘贴成复制出来的代码。注意：不能把代码粘到地址栏直接打开，必须放进书签的网址里。</p>
<p>以后 IMA 过期或知识库不匹配时：打开目标 IMA 知识库，点这个书签御札，在知识库里随便问一句。成功后它会把登录态和当前知识库 ID 发给本机扣子接收结界并自动重启 MCP。</p>
<p>如果你之前拖过旧版书签，请删掉旧的，重新拖一次这个新版。</p>
<script>
const code = {json.dumps(BOOKMARKLET)};
document.getElementById('test').addEventListener('click', () => {{
  alert('测试弹窗正常。下一步：把黑色“更新 IMA 登录态御札”拖到浏览器书签栏。');
}});
document.getElementById('copy').addEventListener('click', async () => {{
  try {{
    await navigator.clipboard.writeText(code);
    alert('御札代码已复制。现在新建一个书签，把网址粘贴成这段代码。');
  }} catch (e) {{
    prompt('复制这段御札代码，粘到书签的网址里：', code);
  }}
}});
</script>
</html>
"""


def log(message: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", "application/json; charset=utf-8")
    handler.send_header("access-control-allow-origin", "https://ima.qq.com")
    handler.send_header("access-control-allow-methods", "GET,POST,OPTIONS")
    handler.send_header("access-control-allow-headers", "content-type")
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def restart_ima_mcp() -> str:
    target = f"gui/{os.getuid()}/{LABEL}"
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", target],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
        check=False,
    )
    if result.returncode == 0:
        return "restarted"
    raise RuntimeError(result.stdout.strip() or f"launchctl exited {result.returncode}")


def update_knowledge_base_env(knowledge_base_id: str) -> None:
    content = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    value = knowledge_base_id.strip()
    if re.search(r"^IMA_KNOWLEDGE_BASE_ID=", content, re.MULTILINE):
        content = re.sub(r"^IMA_KNOWLEDGE_BASE_ID=.*", f"IMA_KNOWLEDGE_BASE_ID={value}", content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\nIMA_KNOWLEDGE_BASE_ID={value}\n"

    if re.search(r"^IMA_KNOWLEDGE_BASE_IDS=", content, re.MULTILINE):
        content = re.sub(r"^IMA_KNOWLEDGE_BASE_IDS=.*", f"IMA_KNOWLEDGE_BASE_IDS={value}", content, flags=re.MULTILINE)

    ENV_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ENV_FILE.write_text(content, encoding="utf-8")
    ENV_FILE.chmod(0o600)


def find_knowledge_base_id(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        text = str(int(value))
        return text if re.fullmatch(r"\d{8,}", text) else None
    if isinstance(value, str):
        match = re.search(
            r"(?:knowledgeBaseId|knowledge_base_id|relatedUrl|kb|wiki|wikis|knowledge)[^0-9]{0,30}(\d{8,})",
            value,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1)
        return value if re.fullmatch(r"\d{8,}", value.strip()) else None
    if isinstance(value, dict):
        for key, item in value.items():
            if re.search(r"knowledge.?base.?id|relatedurl", str(key), flags=re.IGNORECASE):
                direct = find_knowledge_base_id(item)
                if direct:
                    return direct
            nested = find_knowledge_base_id(item)
            if nested:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = find_knowledge_base_id(item)
            if nested:
                return nested
    return None


def extract_credentials(payload: dict) -> tuple[str | None, str | None]:
    bkn = payload.get("bkn")
    cookie = payload.get("cookie")
    if bkn and cookie:
        return str(bkn), str(cookie)
    headers = payload.get("headers")
    if isinstance(headers, str):
        return parse_headers_block(headers)
    return None, None


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexImaAuthReceiver/1.0"

    def log_message(self, format: str, *args: object) -> None:
        log(format % args)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("access-control-allow-origin", "https://ima.qq.com")
        self.send_header("access-control-allow-methods", "GET,POST,OPTIONS")
        self.send_header("access-control-allow-headers", "content-type")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/install"}:
            body = INSTALL_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/health":
            json_response(self, 200, {"ok": True})
            return
        if path == "/ima-auth":
            body = (
                "<!doctype html><meta charset='utf-8'>"
                "<title>IMA 登录态接收结界</title>"
                "<body style='font-family:-apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif;margin:40px;line-height:1.7'>"
                "<h1>这里是接收结界，不能直接打开</h1>"
                "<p><code>/ima-auth</code> 是本机接收接口，不是网页按钮。</p>"
                "<p>请回到 <a href='/install'>安装页</a>，把 <b>javascript:</b> 开头的整段代码保存成书签的网址；"
                "然后在 IMA 知识库页面点那个书签御札。</p>"
                "</body>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        json_response(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/ima-auth":
            json_response(self, 404, {"ok": False, "error": "not found"})
            return

        try:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            bkn, cookie = extract_credentials(payload)
            if not bkn or not cookie:
                json_response(self, 400, {"ok": False, "error": "missing x-ima-bkn or x-ima-cookie"})
                return
            update_env(bkn, cookie)
            update_knowledge_base_env(FIXED_KNOWLEDGE_BASE_ID)
            status = restart_ima_mcp()
            log(f"updated ima auth and restarted mcp; kb={FIXED_KNOWLEDGE_BASE_ID}")
            json_response(self, 200, {"ok": True, "status": status, "knowledgeBaseId": FIXED_KNOWLEDGE_BASE_ID})
        except Exception as exc:
            log(f"update failed: {type(exc).__name__}: {exc}")
            json_response(self, 500, {"ok": False, "error": str(exc)})


def main() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"ima auth receiver listening on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
