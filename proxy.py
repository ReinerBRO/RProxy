#!/usr/bin/env python3
"""
ChatGPT Account Pool Proxy
多 key 计费，按 token 量统计，按 OpenAI 官方价格计算费用
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import urllib.request
import urllib.error

RIKKA_ACCOUNTS = Path("/root/RProxy/rikka_accounts.json")
FREE_ACCOUNTS = Path("/root/RProxy/free_accounts.json")
KEYS_FILE = Path("/root/RProxy/keys.json")
USAGE_FILE = Path("/root/RProxy/usage.json")
ADMIN_PASSWORD = "200414jc"
CHATGPT_BASE = "https://chatgpt.com"
OPENAI_BASE = "https://api.openai.com"
PROXY = "http://127.0.0.1:7890"

# OpenAI 官方价格 ($/1M tokens)
MODEL_PRICES = {
    "gpt-5.4":       {"input": 3.00,  "output": 15.00},
    "gpt-5.3-codex": {"input": 3.00,  "output": 15.00},
    "gpt-4o":        {"input": 2.50,  "output": 10.00},
    "gpt-4o-mini":   {"input": 0.15,  "output": 0.60},
    "gpt-4-turbo":   {"input": 10.00, "output": 30.00},
    "gpt-4":         {"input": 30.00, "output": 60.00},
    "gpt-3.5-turbo": {"input": 0.50,  "output": 1.50},
    "default":       {"input": 3.00,  "output": 15.00},
}

accounts = []
exhausted = set()

# Auto-recovery worker
def recovery_worker():
    """Background thread to check and recover exhausted accounts"""
    while True:
        try:
            time.sleep(300)  # Check every 5 minutes

            if not exhausted:
                continue

            to_recover = []
            for file_name in list(exhausted):
                # Find account in accounts list
                acc = next((a for a in accounts if a["file"] == file_name), None)
                if not acc:
                    continue

                # Query usage to check if limits have reset
                try:
                    token = acc.get("access_token")
                    if not token:
                        continue

                    import urllib.request
                    req = urllib.request.Request(
                        "https://chatgpt.com/backend-api/wham/usage",
                        headers={"Authorization": f"Bearer {token}", "ChatGPT-Account-Id": acc["account_id"]}
                    )
                    resp = urllib.request.urlopen(req, timeout=10)
                    data = json.loads(resp.read().decode())
                    rate_limit = data.get("rate_limit", {})

                    # Check if limits have reset
                    limit_reached = rate_limit.get("limit_reached", False)
                    if not limit_reached:
                        to_recover.append(file_name)
                        print(f"[Recovery] {file_name} limits reset, recovering")
                except Exception as e:
                    print(f"[Recovery] Error checking {file_name}: {e}")

            # Recover accounts
            for file_name in to_recover:
                exhausted.discard(file_name)

        except Exception as e:
            print(f"[Recovery] Worker error: {e}")

# Start recovery worker
recovery_thread = threading.Thread(target=recovery_worker, daemon=True)
recovery_thread.start()
lock = threading.Lock()
current_index = 0

keys: dict = {}   # {api_key: {name, quota_usd, enabled}}
usage: dict = {}  # {api_key: {requests, input_tokens, output_tokens, cost_usd, by_model}}
keys_lock = threading.Lock()


def load_accounts():
    global accounts
    try:
        # Load rikka accounts
        rikka_data = json.loads(RIKKA_ACCOUNTS.read_text())
        rikka_accounts = [{"access_token": a["access_token"], "account_id": a["account_id"], "file": a["file"], "pool": "rikka"} for a in rikka_data]
        
        # Load free accounts
        free_data = json.loads(FREE_ACCOUNTS.read_text())
        free_accounts = [{"access_token": a["access_token"], "account_id": a["account_id"], "file": a["file"], "pool": "free"} for a in free_data]
        
        # Combine: rikka first, then free
        accounts = rikka_accounts + free_accounts
        print(f"Loaded {len(rikka_accounts)} rikka accounts + {len(free_accounts)} free accounts = {len(accounts)} total")
    except Exception as e:
        print(f"Failed to load accounts: {e}")
def load_keys():
    global keys, usage
    if KEYS_FILE.exists():
        keys = json.loads(KEYS_FILE.read_text())
    else:
        keys = {}
        save_keys()
    if USAGE_FILE.exists():
        usage = json.loads(USAGE_FILE.read_text())
    else:
        usage = {}
        save_usage()
    print(f"Loaded {len(keys)} API keys")


def save_keys():
    KEYS_FILE.write_text(json.dumps(keys, indent=2))


def save_usage():
    USAGE_FILE.write_text(json.dumps(usage, indent=2))


def get_next_account(pool="free"):
    global current_index
    with lock:
        # 先按池子过滤，再过滤未耗尽的账号
        pool_accounts = [a for a in accounts if a.get("pool", "free") == pool]
        active = [a for a in pool_accounts if a["file"] not in exhausted]
        if not active:
            return None
        acc = active[current_index % len(active)]
        current_index += 1
        return acc


def mark_exhausted(file_name):
    with lock:
        exhausted.add(file_name)
        active = len(accounts) - len(exhausted)
        print(f"  -> exhausted: {file_name[:30]} (active: {active})")


def calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price = MODEL_PRICES.get(model, MODEL_PRICES["default"])
    return (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000


def record_usage(api_key: str, model: str, input_tokens: int, output_tokens: int):
    cost = calc_cost(model, input_tokens, output_tokens)
    with keys_lock:
        if api_key not in usage:
            usage[api_key] = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "by_model": {}}
        u = usage[api_key]
        u["requests"] += 1
        u["input_tokens"] += input_tokens
        u["output_tokens"] += output_tokens
        u["cost_usd"] = round(u["cost_usd"] + cost, 8)
        if model not in u["by_model"]:
            u["by_model"][model] = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        m = u["by_model"][model]
        m["requests"] += 1
        m["input_tokens"] += input_tokens
        m["output_tokens"] += output_tokens
        m["cost_usd"] = round(m["cost_usd"] + cost, 8)
        save_usage()


def parse_tokens_from_response(data: bytes) -> tuple[int, int, str]:
    """从响应体解析 input/output token 数和模型名"""
    input_tokens = output_tokens = 0
    model = "default"
    try:
        # 尝试 JSON（非流式）
        obj = json.loads(data.decode())
        usage_obj = obj.get("usage", {})
        input_tokens = usage_obj.get("input_tokens", usage_obj.get("prompt_tokens", 0))
        output_tokens = usage_obj.get("output_tokens", usage_obj.get("completion_tokens", 0))
        model = obj.get("model", "default")
    except Exception:
        # SSE 流式：扫描 data: {...} 行
        for line in data.split(b"\n"):
            if not line.startswith(b"data:"):
                continue
            raw = line[5:].strip()
            if raw in (b"[DONE]", b""):
                continue
            try:
                obj = json.loads(raw)
                # Responses API usage event
                if obj.get("type") == "response.completed":
                    u = obj.get("response", {}).get("usage", {})
                    input_tokens = u.get("input_tokens", 0)
                    output_tokens = u.get("output_tokens", 0)
                    model = obj.get("response", {}).get("model", model)
                # Chat completions usage chunk
                u = obj.get("usage") or {}
                if u.get("prompt_tokens"):
                    input_tokens = u["prompt_tokens"]
                    output_tokens = u.get("completion_tokens", 0)
                if obj.get("model"):
                    model = obj["model"]
            except Exception:
                pass
    return input_tokens, output_tokens, model


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {format % args}")

    def do_GET(self):
        print(f"[DEBUG] do_GET path: {self.path!r}")
        if self.path == "/" or self.path == "":
            self._json({"service": "ChatGPT Proxy", "status": "running", "version": "1.0"})
            return
        if self.path == "/health":
            # 统计各个池的账号数
            pools_stat = {}
            for pool_name in ["free", "rikka"]:
                pool_accounts = [a for a in accounts if a.get("pool", "free") == pool_name]
                pool_exhausted = [a for a in pool_accounts if a["file"] in exhausted]
                pools_stat[pool_name] = {
                    "total": len(pool_accounts),
                    "exhausted": len(pool_exhausted),
                    "active": len(pool_accounts) - len(pool_exhausted)
                }
            self._json({
                "status": "ok", 
                "accounts": len(accounts), 
                "exhausted": len(exhausted), 
                "active": len(accounts) - len(exhausted),
                "pools": pools_stat
            })
            return
        if self.path == "/status":
            self._handle_status()
            return
        if self.path == "/v1":
            self._json({"object": "api", "status": "ok"})
            return
        if self.path == "/v1/models":
            self._handle_models()
            return
        if self.path.startswith("/admin"):
            self._handle_admin()
            return
        # 对于未知的GET请求，返回友好的错误而不是代理
        self._send_error(404, f"endpoint not found: {self.path}")

    def do_POST(self):
        print(f"[REQUEST] POST {self.path} from {self.client_address[0]}")
        if self.path.startswith("/admin"):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            self._handle_admin(body)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self._proxy_request(body)

    def do_DELETE(self):
        if self.path.startswith("/admin"):
            self._handle_admin(b"")
            return
        self._send_error(405, "method not allowed")

    def do_PATCH(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        if self.path.startswith("/admin"):
            self._handle_admin(body)
            return
        self._send_error(405, "method not allowed")

    def _auth_key(self):
        """验证请求的 API key，返回 key 字符串或 None"""
        # 支持两种认证方式：Authorization Bearer 和 x-api-key
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            key = auth[7:]
        else:
            key = self.headers.get("x-api-key", "")
            if not key:
                return None
        with keys_lock:
            info = keys.get(key)
        if not info or not info.get("enabled", True):
            return None
        # 检查额度
        quota = info.get("quota_usd", 0)
        if quota > 0:
            with keys_lock:
                used = usage.get(key, {}).get("cost_usd", 0.0)
            if used >= quota:
                return "QUOTA_EXCEEDED"
        return key

    def _proxy_request(self, body: bytes):
        key = self._auth_key()
        if key is None:
            self._send_error(401, "invalid api key")
            return
        if key == "QUOTA_EXCEEDED":
            self._send_error(429, "quota exceeded")
            return
        
        # 获取该 key 对应的 pool
        with keys_lock:
            key_info = keys.get(key, {})
            pool = key_info.get("pool", "free")

        # 调试：记录请求
        if self.path == "/responses" or self.path == "/v1/responses":
            try:
                req_data = json.loads(body.decode()) if body else {}
                print(f"[DEBUG] /responses request keys: {list(req_data.keys())}")

                # 如果缺少 instructions 字段，添加一个空的
                if "instructions" not in req_data:
                    req_data["instructions"] = ""

                # 移除不支持的参数
                unsupported = ["max_output_tokens", "max_tokens", "temperature", "top_p", "frequency_penalty", "presence_penalty"]
                for param in unsupported:
                    if param in req_data:
                        del req_data[param]
                        print(f"[DEBUG] Removed unsupported parameter: {param}")

                body = json.dumps(req_data).encode()
            except Exception as e:
                print(f"[DEBUG] Error processing request: {e}")

        # Chat Completions → Responses API 转换
        if self.path == "/v1/chat/completions":
            self._handle_chat_completions(body, key, pool)
            return

        # Anthropic Messages API → Responses API 转换
        if self.path == "/v1/messages":
            self._handle_anthropic_messages(body, key, pool)
            return

        proxy_handler = urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
        opener = urllib.request.build_opener(proxy_handler)

        for attempt in range(5):
            acc = get_next_account(pool)
            if not acc:
                self._send_error(503, "no accounts available")
                return

            if self.path == "/v1/responses" or self.path == "/responses":
                url = "https://chatgpt.com/backend-api/codex/responses"
            elif self.path.startswith("/v1/"):
                url = OPENAI_BASE + self.path
            else:
                url = CHATGPT_BASE + self.path

            headers = {
                "Content-Type": self.headers.get("Content-Type", "application/json"),
                "Authorization": f"Bearer {acc['access_token']}",
                "ChatGPT-Account-Id": acc["account_id"],
                "User-Agent": "codexs/0.3",
                "Accept": self.headers.get("Accept", "text/event-stream"),
            }
            req = urllib.request.Request(url, data=body if body else None, headers=headers, method=self.command)

            try:
                resp = opener.open(req, timeout=60)
                print(f"[DEBUG] Response status: {resp.status}, Content-Type: {resp.headers.get('Content-Type')}")
                data = resp.read()
                print(f"[DEBUG] Read {len(data)} bytes")
                # 解析 token 并记录用量
                input_t, output_t, model = parse_tokens_from_response(data)
                if input_t or output_t:
                    record_usage(key, model, input_t, output_t)
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)
                print(f"  -> OK {resp.status} key={key[:16]}... in={input_t} out={output_t} model={model}")
                return
            except urllib.error.HTTPError as e:
                data = e.read()
                print(f"[DEBUG] HTTPError {e.code}: {data[:200]}")
                try:
                    err_json = json.loads(data.decode())
                    err_type = err_json.get("error", {}).get("type", "")
                    err_code = err_json.get("error", {}).get("code", "")
                    if err_type == "insufficient_quota" or err_code == "insufficient_quota" or e.code == 429:
                        mark_exhausted(acc["file"])
                        print(f"  -> quota exhausted, retrying (attempt {attempt+1})")
                        continue
                except Exception:
                    pass
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception as e:
                self._send_error(502, str(e))
                return

        self._send_error(503, "all retried accounts exhausted")

    def _is_admin_authed(self) -> bool:
        cookie = self.headers.get("Cookie", "")
        return "admin_session=1" in cookie

    def _handle_admin(self, body: bytes = b""):
        # POST /admin/login — 密码登录
        if self.command == "POST" and self.path == "/admin/login":
            try:
                pwd = json.loads(body.decode()).get("password", "")
            except Exception:
                pwd = ""
            if pwd == ADMIN_PASSWORD:
                resp = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", "admin_session=1; Path=/admin; HttpOnly")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            else:
                self._send_error(403, "wrong password")
            return

        if not self._is_admin_authed():
            self._serve_login_page()
            return

        # GET /admin/keys — 列出所有 key
        if self.command == "GET" and self.path == "/admin/keys":
            with keys_lock:
                result = []
                for k, info in keys.items():
                    u = usage.get(k, {})
                    result.append({
                        "key": k,
                        "name": info.get("name", ""),
                        "quota_usd": info.get("quota_usd", 0),
                        "enabled": info.get("enabled", True),
                        "requests": u.get("requests", 0),
                        "input_tokens": u.get("input_tokens", 0),
                        "output_tokens": u.get("output_tokens", 0),
                        "cost_usd": u.get("cost_usd", 0.0),
                    })
            self._json(result)
            return

        # GET /admin/usage — 详细用量
        if self.command == "GET" and self.path == "/admin/usage":
            with keys_lock:
                result = {}
                for k, info in keys.items():
                    result[k] = {"name": info.get("name", ""), **usage.get(k, {})}
            self._json(result)
            return

        # GET /admin — HTML 面板
        if self.command == "GET" and self.path in ("/admin", "/admin/"):
            self._handle_admin_html()
            return

        # POST /admin/keys — 创建/更新 key
        if self.command == "POST" and self.path == "/admin/keys":
            try:
                req = json.loads(body.decode())
                k = req["key"]
                with keys_lock:
                    keys[k] = {
                        "name": req.get("name", ""),
                        "quota_usd": float(req.get("quota_usd", 0)),
                        "enabled": req.get("enabled", True),
                        "pool": req.get("pool", "free"),
                    }
                    save_keys()
                self._json({"success": True, "key": k})
            except Exception as e:
                self._send_error(400, str(e))
            return

        # DELETE /admin/keys/<key>
        if self.command == "DELETE" and self.path.startswith("/admin/keys/"):
            k = self.path[len("/admin/keys/"):]
            with keys_lock:
                removed = keys.pop(k, None)
                if removed:
                    save_keys()
            self._json({"success": bool(removed)})
            return

        # PATCH /admin/keys/<key> — 更新 pool / enabled
        if self.command == "PATCH" and self.path.startswith("/admin/keys/"):
            k = self.path[len("/admin/keys/"):]
            try:
                req = json.loads(body.decode())
                with keys_lock:
                    if k not in keys:
                        self._send_error(404, "key not found")
                        return
                    if "pool" in req:
                        keys[k]["pool"] = req["pool"]
                    if "enabled" in req:
                        keys[k]["enabled"] = req["enabled"]
                    save_keys()
                self._json({"success": True})
            except Exception as e:
                self._send_error(400, str(e))
            return

        self._send_error(404, "not found")

    def _serve_login_page(self):
        html = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>Proxy Admin</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Syne:wght@400;700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0a0a0f;--surface:#12121a;--border:#2a2a3a;--accent:#00e5ff;--accent2:#ff3d71;--text:#e0e0f0;--muted:#555570}
body{background:var(--bg);color:var(--text);font-family:'Share Tech Mono',monospace;min-height:100vh;display:flex;align-items:center;justify-content:center;overflow:hidden}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 80% 60% at 50% 0%,rgba(0,229,255,.07) 0%,transparent 70%);pointer-events:none}
.grid-bg{position:fixed;inset:0;background-image:linear-gradient(rgba(0,229,255,.03) 1px,transparent 1px),linear-gradient(90deg,rgba(0,229,255,.03) 1px,transparent 1px);background-size:40px 40px;pointer-events:none}
.card{position:relative;background:var(--surface);border:1px solid var(--border);padding:48px 40px;width:360px;clip-path:polygon(0 0,calc(100% - 20px) 0,100% 20px,100% 100%,0 100%)}
.card::before{content:'';position:absolute;top:0;right:0;width:20px;height:20px;background:var(--accent);clip-path:polygon(0 0,100% 100%,100% 0)}
.logo{font-family:'Syne',sans-serif;font-weight:800;font-size:11px;letter-spacing:.3em;color:var(--accent);text-transform:uppercase;margin-bottom:32px;opacity:.8}
h1{font-family:'Syne',sans-serif;font-weight:700;font-size:22px;margin-bottom:8px;letter-spacing:-.01em}
.sub{font-size:11px;color:var(--muted);margin-bottom:32px;letter-spacing:.05em}
.field{position:relative;margin-bottom:20px}
.field label{display:block;font-size:10px;letter-spacing:.15em;color:var(--muted);text-transform:uppercase;margin-bottom:8px}
.field input{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:'Share Tech Mono',monospace;font-size:14px;padding:12px 16px;outline:none;transition:border-color .2s}
.field input:focus{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.btn{width:100%;background:var(--accent);color:#000;font-family:'Syne',sans-serif;font-weight:700;font-size:13px;letter-spacing:.1em;text-transform:uppercase;border:none;padding:14px;cursor:pointer;transition:opacity .15s,transform .1s;clip-path:polygon(0 0,calc(100% - 10px) 0,100% 10px,100% 100%,0 100%)}
.btn:hover{opacity:.85}
.btn:active{transform:scale(.98)}
.err{color:var(--accent2);font-size:11px;margin-top:12px;min-height:16px;letter-spacing:.05em}
.scan{position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--accent),transparent);animation:scan 3s ease-in-out infinite;opacity:.4}
@keyframes scan{0%,100%{transform:translateY(0)}50%{transform:translateY(200px)}}
</style></head>
<body>
<div class="grid-bg"></div>
<div class="card">
  <div class="scan"></div>
  <div class="logo">Codex Pool</div>
  <h1>Admin Access</h1>
  <p class="sub">AUTHENTICATION REQUIRED</p>
  <div class="field">
    <label>Password</label>
    <input type="password" id="p" placeholder="••••••••" onkeydown="if(event.key==='Enter')login()">
  </div>
  <button class="btn" onclick="login()">Authenticate →</button>
  <div class="err" id="err"></div>
</div>
<script>
async function login(){
  const r=await fetch('/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({password:document.getElementById('p').value})});
  if(r.ok){location.reload();}
  else{const e=document.getElementById('err');e.textContent='ACCESS DENIED';setTimeout(()=>e.textContent='',2000);}
}
</script></body></html>"""
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_admin_html(self):
        with keys_lock:
            rows_json = json.dumps([
                {
                    "key": k,
                    "name": info.get("name", ""),
                    "quota": info.get("quota_usd", 0),
                    "enabled": info.get("enabled", True),
                    "pool": info.get("pool", "free"),
                    "requests": usage.get(k, {}).get("requests", 0),
                    "input": usage.get(k, {}).get("input_tokens", 0),
                    "output": usage.get(k, {}).get("output_tokens", 0),
                    "cost": usage.get(k, {}).get("cost_usd", 0.0),
                }
                for k, info in keys.items()
            ])
        total_keys = len(keys)
        html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>Proxy Admin</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#f7f8fa;--surface:#fff;--surface2:#f0f1f5;--border:#e2e4ea;--border2:#d0d3dc;
  --accent:#2563eb;--accent-light:#eff4ff;--red:#ef4444;--red-light:#fef2f2;
  --green:#16a34a;--green-light:#f0fdf4;--yellow:#d97706;--yellow-light:#fffbeb;
  --text:#111827;--muted:#6b7280;--muted2:#9ca3af
}}
body{{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;min-height:100vh}}
.topbar{{background:var(--surface);border-bottom:1px solid var(--border);padding:0 32px;height:56px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10}}
.brand{{display:flex;align-items:center;gap:10px}}
.brand-icon{{width:28px;height:28px;background:var(--accent);border-radius:6px;display:flex;align-items:center;justify-content:center}}
.brand-icon svg{{width:14px;height:14px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}}
.brand-name{{font-weight:700;font-size:15px;letter-spacing:-.02em}}
.brand-tag{{font-size:11px;color:var(--muted);background:var(--surface2);padding:2px 8px;border-radius:20px;border:1px solid var(--border)}}
.wrap{{max-width:1100px;margin:0 auto;padding:32px 24px}}
.page-title{{font-size:22px;font-weight:700;letter-spacing:-.03em;margin-bottom:4px}}
.page-sub{{font-size:13px;color:var(--muted);margin-bottom:28px}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:28px;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
.card-header{{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}}
.card-title{{font-size:13px;font-weight:600;letter-spacing:-.01em}}
.badge-count{{background:var(--surface2);border:1px solid var(--border);border-radius:20px;padding:2px 10px;font-size:11px;color:var(--muted);font-weight:500}}
table{{width:100%;border-collapse:collapse}}
th{{padding:10px 16px;font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);text-align:left;background:var(--surface2);border-bottom:1px solid var(--border)}}
td{{padding:13px 16px;font-size:13px;border-bottom:1px solid var(--border);vertical-align:middle}}
tbody tr:last-child td{{border-bottom:none}}
tbody tr:hover td{{background:#fafbfc}}
.name-cell{{font-weight:600;font-size:13px}}
.key-cell{{display:flex;align-items:center;gap:8px}}
.key-mono{{font-family:'DM Mono',monospace;font-size:12px;color:var(--muted);letter-spacing:.02em}}
.copy-btn{{display:inline-flex;align-items:center;gap:4px;background:var(--surface2);border:1px solid var(--border2);color:var(--muted);padding:3px 10px;font-size:11px;font-family:'DM Sans',sans-serif;font-weight:500;border-radius:5px;cursor:pointer;transition:all .15s;white-space:nowrap}}
.copy-btn:hover{{background:var(--accent-light);border-color:var(--accent);color:var(--accent)}}
.copy-btn.copied{{background:var(--green-light);border-color:var(--green);color:var(--green)}}
.copy-btn svg{{width:11px;height:11px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}}
.status-dot{{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:500}}
.status-dot::before{{content:'';width:6px;height:6px;border-radius:50%;background:currentColor}}
.status-dot.on{{color:var(--green)}}
.status-dot.off{{color:var(--red)}}
.quota-cell{{min-width:140px}}
.quota-nums{{font-size:12px;color:var(--muted);margin-bottom:5px;font-family:'DM Mono',monospace}}
.quota-track{{height:4px;background:var(--surface2);border-radius:2px;overflow:hidden}}
.quota-fill{{height:100%;border-radius:2px;transition:width .3s}}
.quota-fill.ok{{background:var(--accent)}}
.quota-fill.warn{{background:var(--yellow)}}
.quota-fill.danger{{background:var(--red)}}
.num-cell{{font-family:'DM Mono',monospace;font-size:12px;color:var(--muted)}}
.form-card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
.form-row{{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}}
.field{{display:flex;flex-direction:column;gap:5px}}
.field label{{font-size:11px;font-weight:600;color:var(--muted);letter-spacing:.04em;text-transform:uppercase}}
.field input{{background:var(--bg);border:1px solid var(--border2);color:var(--text);font-family:'DM Mono',monospace;font-size:13px;padding:8px 12px;border-radius:6px;outline:none;transition:border-color .15s,box-shadow .15s}}
.field input:focus{{border-color:var(--accent);box-shadow:0 0 0 3px rgba(37,99,235,.1)}}
.field.grow{{flex:1;min-width:200px}}
.save-btn{{background:var(--accent);color:#fff;font-family:'DM Sans',sans-serif;font-weight:600;font-size:13px;border:none;padding:9px 20px;border-radius:6px;cursor:pointer;white-space:nowrap;transition:opacity .15s}}
.save-btn:hover{{opacity:.88}}
.pool-sel{{background:var(--bg);border:1px solid var(--border2);color:var(--text);font-size:12px;padding:4px 8px;border-radius:5px;cursor:pointer;font-family:'DM Sans',sans-serif}}
.del-btn{{background:var(--red-light);border:1px solid var(--red);color:var(--red);font-size:11px;font-weight:600;padding:4px 10px;border-radius:5px;cursor:pointer;font-family:'DM Sans',sans-serif;transition:opacity .15s}}
.del-btn:hover{{opacity:.75}}
.toast{{position:fixed;bottom:24px;right:24px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px 18px;font-size:13px;box-shadow:0 4px 16px rgba(0,0,0,.1);opacity:0;transform:translateY(6px);transition:all .2s;pointer-events:none;z-index:100;display:flex;align-items:center;gap:8px}}
.toast.show{{opacity:1;transform:translateY(0)}}
.toast.ok{{border-color:var(--green);color:var(--green)}}
.toast.err{{border-color:var(--red);color:var(--red)}}
</style></head>
<body>
<div class="topbar">
  <div class="brand">
    <div class="brand-icon"><svg viewBox="0 0 24 24"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg></div>
    <span class="brand-name">Codex Pool</span>
    <span class="brand-tag">Admin</span>
  </div>
  <span style="font-size:12px;color:var(--muted)">{total_keys} keys</span>
</div>
<div class="wrap">
  <div class="page-title">API Key Management</div>
  <div class="page-sub">Manage access keys, quotas, and monitor usage</div>

  <div class="card">
    <div class="card-header">
      <span class="card-title">Keys</span>
      <span class="badge-count" id="key-count">{total_keys}</span>
    </div>
    <table>
      <thead><tr>
        <th>Name</th><th>Key</th><th>Pool</th><th>Quota / Cost</th>
        <th>Requests</th><th>Input</th><th>Output</th><th>Status</th><th></th>
      </tr></thead>
      <tbody id="keys-tbody"></tbody>
    </table>
  </div>

  <div class="form-card">
    <div style="font-size:13px;font-weight:600;margin-bottom:14px">Create New Key</div>
    <div class="form-row">
      <div class="field"><label>Name</label><input id="f-name" placeholder="user-01" style="width:160px"></div>
      <div class="field"><label>Quota ($)</label><input id="f-quota" placeholder="0 = unlimited" style="width:140px" value="0"></div>
      <button class="save-btn" onclick="createKey()">Generate Key</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
const ROWS={rows_json};
function maskKey(k){{
  if(k.length<=12)return k;
  return k.slice(0,6)+'••••••••••••'+k.slice(-4);
}}
function fmt(n){{return n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':String(n)}}
function copyText(text,btn){{
  if(navigator.clipboard&&window.isSecureContext){{
    navigator.clipboard.writeText(text).then(()=>flashCopied(btn)).catch(()=>fallbackCopy(text,btn));
  }}else{{fallbackCopy(text,btn);}}
}}
function fallbackCopy(text,btn){{
  const ta=document.createElement('textarea');
  ta.value=text;ta.style.cssText='position:fixed;opacity:0;top:0;left:0';
  document.body.appendChild(ta);ta.focus();ta.select();
  try{{document.execCommand('copy');flashCopied(btn);}}catch(e){{}}
  document.body.removeChild(ta);
}}
function flashCopied(btn){{
  btn.innerHTML='<svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg> Copied';
  btn.classList.add('copied');
  setTimeout(()=>{{btn.innerHTML='<svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg> Copy';btn.classList.remove('copied');}},1500);
}}
function renderRows(){{
  const tbody=document.getElementById('keys-tbody');
  tbody.innerHTML=ROWS.map(r=>{{
    const pct=r.quota>0?Math.min(100,r.cost/r.quota*100):0;
    const fc=pct>90?'danger':pct>70?'warn':'ok';
    const qHtml=r.quota>0
      ?`<div class="quota-cell"><div class="quota-nums">💲${{r.cost.toFixed(4)}} / ${{r.quota.toFixed(2)}}</div><div class="quota-track"><div class="quota-fill ${{fc}}" style="width:${{pct.toFixed(1)}}%"></div></div></div>`
      :`<span class="num-cell">💲${{r.cost.toFixed(4)}} / ∞</span>`;
    const copyIcon='<svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
    return `<tr>
      <td class="name-cell">${{r.name||'—'}}</td>
      <td><div class="key-cell">
        <span class="key-mono">${{maskKey(r.key)}}</span>
        <button class="copy-btn" onclick="copyText('${{r.key}}',this)">${{copyIcon}} Copy</button>
      </div></td>
      <td>${{qHtml}}</td>
      <td class="num-cell">${{fmt(r.requests)}}</td>
      <td class="num-cell">${{fmt(r.input)}}</td>
      <td class="num-cell">${{fmt(r.output)}}</td>
      <td><span class="status-dot ${{r.enabled?'on':'off'}}">${{r.enabled?'Active':'Disabled'}}</span></td>
    </tr>`;
  }}).join('');
}}
function toast(msg,type='ok'){{
  const t=document.getElementById('toast');
  t.textContent=msg;t.className='toast show '+type;
  setTimeout(()=>t.className='toast',2200);
}}
async function changePool(key,pool){{
  const r=await fetch('/admin/keys/'+encodeURIComponent(key),{{method:'PATCH',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{pool}})}});
  if(r.ok)toast('Pool updated ✓');else toast('Error','err');
}}
async function deleteKey(key,btn){{
  if(!confirm('Delete this key?'))return;
  const r=await fetch('/admin/keys/'+encodeURIComponent(key),{{method:'DELETE'}});
  if(r.ok){{toast('Deleted');btn.closest('tr').remove();}}else toast('Error','err');
}}
async function createKey(){{
  const name=document.getElementById('f-name').value.trim();
  const quota=parseFloat(document.getElementById('f-quota').value)||0;
  if(!name){{toast('Name is required','err');return;}}
  // 生成随机 key: sk- + 48位随机字符
  const chars='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  const rand=Array.from(crypto.getRandomValues(new Uint8Array(48))).map(b=>chars[b%chars.length]).join('');
  const key='sk-'+rand;
  const r=await fetch('/admin/keys',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    const pool=document.getElementById('f-pool').value;
    body:JSON.stringify({{key,name,quota_usd:quota,pool}})}});
  if(r.ok){{
    toast('Key created ✓');
    document.getElementById('f-name').value='';
    setTimeout(()=>location.reload(),800);
  }}else toast('Error creating key','err');
}}
renderRows();
</script></body></html>"""
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_status(self):
        proxy_handler = urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})

        def query_usage(acc):
            status = "exhausted" if acc["file"] in exhausted else "active"
            used_pct = None
            limit_5h = None
            limit_week = None
            plan_type = "unknown"
            
            # For rikka pool, always query detailed info regardless of status
            try:
                opener = urllib.request.build_opener(proxy_handler)
                req = urllib.request.Request(
                    "https://chatgpt.com/backend-api/wham/usage",
                    headers={"Authorization": f"Bearer {acc['access_token']}", "ChatGPT-Account-Id": acc["account_id"], "User-Agent": "codexs/0.3"}
                )
                r = opener.open(req, timeout=6)
                d = json.loads(r.read().decode())
                rl = d.get("rate_limit", {})
                pw = rl.get("primary_window", {})
                used_pct = pw.get("used_percent", 0)
                plan_type = d.get("plan_type", "unknown")
                
                # primary_window = 5h rolling, secondary_window = week rolling
                pw = rl.get("primary_window", {})
                sw = rl.get("secondary_window")
                if pw:
                    limit_5h = {
                        "percent": pw.get("used_percent", 0),
                        "reset_at": pw.get("reset_at", 0),
                        "reset_after": pw.get("reset_after_seconds", 0)
                    }
                if sw:
                    limit_week = {
                        "percent": sw.get("used_percent", 0),
                        "reset_at": sw.get("reset_at", 0),
                        "reset_after": sw.get("reset_after_seconds", 0)
                    }
                
                if rl.get("limit_reached"):
                    status = "exhausted"
                    mark_exhausted(acc["file"])
            except Exception as e:
                print(f"[Status] Error querying {acc['file']}: {e}")
                used_pct = None
            return {
                "file": acc["file"], 
                "status": status, 
                "used_pct": used_pct, 
                "pool": acc.get("pool", "free"),
                "limit_5h": limit_5h,
                "limit_week": limit_week,
                "plan_type": plan_type
            }

        # Query rikka pool for detailed status, free pool shows basic info
        rows = []
        rikka_accounts = [(i, a) for i, a in enumerate(accounts) if a.get("pool") == "rikka"]

        # Query rikka accounts
        rikka_results = {}
        if rikka_accounts:
            with ThreadPoolExecutor(max_workers=50) as pool:
                futures = {pool.submit(query_usage, acc): idx for idx, acc in rikka_accounts}
                for f in as_completed(futures):
                    rikka_results[futures[f]] = f.result()

        # Build rows: detailed for rikka, basic for free
        for i, acc in enumerate(accounts):
            if i in rikka_results:
                rows.append(rikka_results[i])
            else:
                # Free pool: basic info only
                rows.append({
                    "file": acc["file"],
                    "status": "active" if acc.get("access_token") else "inactive",
                    "used_pct": 0,
                    "pool": acc.get("pool", "free"),
                    "limit_5h": None,
                    "limit_week": None,
                    "plan_type": "free"
                })

        # 按池分组
        free_rows = [(i, r) for i, r in enumerate(rows) if r.get('pool', 'free') == 'free']
        rikka_rows = [(i, r) for i, r in enumerate(rows) if r.get('pool', 'free') == 'rikka']
        
        free_active = len([r for i, r in free_rows if r['status']=='active'])
        free_exhausted = len([r for i, r in free_rows if r['status']=='exhausted'])
        rikka_active = len([r for i, r in rikka_rows if r['status']=='active'])
        rikka_exhausted = len([r for i, r in rikka_rows if r['status']=='exhausted'])
        
        html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>Account Pool Status</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#f7f8fa;--surface:#fff;--surface2:#f0f1f5;--border:#e2e4ea;--border2:#d0d3dc;--accent:#2563eb;--accent-light:#eff4ff;--red:#ef4444;--red-light:#fef2f2;--green:#16a34a;--green-light:#f0fdf4;--yellow:#d97706;--yellow-light:#fffbeb;--text:#111827;--muted:#6b7280;--muted2:#9ca3af;--free:#2196F3;--rikka:#FF9800}}
body{{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;min-height:100vh}}
.topbar{{background:var(--surface);border-bottom:1px solid var(--border);padding:0 32px;height:56px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10}}
.brand{{display:flex;align-items:center;gap:10px}}
.brand-icon{{width:28px;height:28px;background:var(--accent);border-radius:6px;display:flex;align-items:center;justify-content:center}}
.brand-icon svg{{width:14px;height:14px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}}
.brand-name{{font-weight:700;font-size:15px;letter-spacing:-.02em}}
.brand-tag{{font-size:11px;color:var(--muted);background:var(--surface2);padding:2px 8px;border-radius:20px;border:1px solid var(--border)}}
.wrap{{max-width:1200px;margin:0 auto;padding:32px 24px}}
.page-title{{font-size:22px;font-weight:700;letter-spacing:-.03em;margin-bottom:4px}}
.page-sub{{font-size:13px;color:var(--muted);margin-bottom:28px}}
.pool-selector{{display:flex;gap:12px;margin-bottom:28px;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:8px;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
.pool-btn{{flex:1;background:transparent;border:none;padding:12px 20px;font-family:'DM Sans',sans-serif;font-size:14px;font-weight:600;color:var(--muted);border-radius:6px;cursor:pointer;transition:all .15s;display:flex;align-items:center;justify-content:center;gap:8px}}
.pool-btn:hover{{background:var(--surface2)}}
.pool-btn.active{{background:var(--accent-light);color:var(--accent);box-shadow:0 1px 3px rgba(37,99,235,.1)}}
.pool-btn.active.free{{background:#e3f2fd;color:var(--free)}}
.pool-btn.active.rikka{{background:#fff3e0;color:var(--rikka)}}
.pool-badge{{font-size:11px;background:currentColor;color:#fff;padding:2px 8px;border-radius:12px;font-weight:500;opacity:.8}}
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:28px}}
.stat-card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 20px;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
.stat-label{{font-size:11px;font-weight:600;color:var(--muted);letter-spacing:.04em;text-transform:uppercase;margin-bottom:8px}}
.stat-value{{font-size:28px;font-weight:700;letter-spacing:-.02em}}
.stat-value.green{{color:var(--green)}}
.stat-value.red{{color:var(--red)}}
.stat-value.blue{{color:var(--accent)}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
.card-header{{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}}
.card-title{{font-size:13px;font-weight:600;letter-spacing:-.01em}}
.badge-count{{background:var(--surface2);border:1px solid var(--border);border-radius:20px;padding:2px 10px;font-size:11px;color:var(--muted);font-weight:500}}
table{{width:100%;border-collapse:collapse}}
th{{padding:10px 16px;font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);text-align:left;background:var(--surface2);border-bottom:1px solid var(--border)}}
td{{padding:13px 16px;font-size:13px;border-bottom:1px solid var(--border);vertical-align:middle}}
tbody tr:last-child td{{border-bottom:none}}
tbody tr:hover td{{background:#fafbfc}}
tr.active{{background:var(--green-light)}}
tr.exhausted{{background:var(--red-light)}}
.status-badge{{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:500;padding:4px 10px;border-radius:12px}}
.status-badge.active{{background:var(--green-light);color:var(--green)}}
.status-badge.exhausted{{background:var(--red-light);color:var(--red)}}
.status-badge::before{{content:'';width:6px;height:6px;border-radius:50%;background:currentColor}}
.usage-bar{{height:6px;background:var(--surface2);border-radius:3px;overflow:hidden;margin-top:4px}}
.usage-fill{{height:100%;background:var(--green);border-radius:3px;transition:width .3s}}
.usage-fill.warn{{background:var(--yellow)}}
.usage-fill.danger{{background:var(--red)}}
.pool-section{{display:none}}
.pool-section.active{{display:block}}
</style></head>
<body>
<div class="topbar">
  <div class="brand">
    <div class="brand-icon"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg></div>
    <span class="brand-name">Account Pool</span>
    <span class="brand-tag">Status</span>
  </div>
  <span style="font-size:12px;color:var(--muted)">{len(accounts)} accounts</span>
</div>
<div class="wrap">
  <div class="page-title">Account Pool Status</div>
  <div class="page-sub">Monitor account usage and availability</div>
  <div class="pool-selector">
    <button class="pool-btn free" onclick="switchPool('free')">
      <span>Free Pool</span>
      <span class="pool-badge">{len(free_rows)}</span>
    </button>
    <button class="pool-btn active rikka" onclick="switchPool('rikka')">
      <span>Rikka Pool</span>
      <span class="pool-badge">{len(rikka_rows)}</span>
    </button>
  </div>
  <div class="pool-section" id="free-section">
    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-label">Total Accounts</div>
        <div class="stat-value blue">{len(free_rows)}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Active</div>
        <div class="stat-value green">{free_active}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Exhausted</div>
        <div class="stat-value red">{free_exhausted}</div>
      </div>
    </div>
    <div class="card">
      <div class="card-header">
        <span class="card-title">Free Pool Accounts</span>
        <span class="badge-count">{len(free_rows)}</span>
      </div>
      <table>
        <thead><tr>
          <th>#</th><th>Account File</th><th>Status</th><th>Usage</th>
        </tr></thead>
        <tbody>
"""
        
        for i, r in free_rows:
            used_pct = r['used_pct'] if r['used_pct'] is not None else 0
            bar_class = 'danger' if used_pct > 90 else 'warn' if used_pct > 70 else ''
            usage_html = f"<div style='min-width:120px'><div style='font-family:DM Mono,monospace;font-size:12px;color:var(--muted)'>{used_pct}%</div><div class='usage-bar'><div class='usage-fill {bar_class}' style='width:{used_pct}%'></div></div></div>" if r['used_pct'] is not None else "<span style='color:var(--muted)'>?</span>"
            html += f"<tr class='{r['status']}'><td>{i+1}</td><td style='font-family:DM Mono,monospace;font-size:12px'>{r['file']}</td><td><span class='status-badge {r['status']}'>{r['status'].title()}</span></td><td>{usage_html}</td></tr>"
        
        html += f"""
        </tbody>
      </table>
    </div>
  </div>
  <div class="pool-section active" id="rikka-section">
    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-label">Total Accounts</div>
        <div class="stat-value blue">{len(rikka_rows)}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Active</div>
        <div class="stat-value green">{rikka_active}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Exhausted</div>
        <div class="stat-value red">{rikka_exhausted}</div>
      </div>
    </div>
    <div class="card">
      <div class="card-header">
        <span class="card-title">Rikka Pool Accounts</span>
        <span class="badge-count">{len(rikka_rows)}</span>
      </div>
      <table>
        <thead><tr>
          <th>#</th><th>Account File</th><th>Plan</th><th>Status</th><th>5h Limit</th><th>Week Limit</th>
        </tr></thead>
        <tbody>
"""
        
        for i, r in rikka_rows:
            # 5h limit
            limit_5h = r.get('limit_5h')
            if limit_5h:
                limit_5h_pct = limit_5h.get('percent', 0)
                bar_class_5h = 'danger' if limit_5h_pct > 90 else 'warn' if limit_5h_pct > 70 else ''
                reset_at_5h = limit_5h.get('reset_at', 0)
                from datetime import datetime
                reset_time_5h = datetime.fromtimestamp(reset_at_5h).strftime('%m-%d %H:%M') if reset_at_5h else 'N/A'
                limit_5h_html = f"<div style='min-width:140px'><div style='font-family:DM Mono,monospace;font-size:11px;color:var(--muted)'>{limit_5h_pct:.0f}%</div><div class='usage-bar'><div class='usage-fill {bar_class_5h}' style='width:{min(limit_5h_pct,100)}%'></div></div><div style='font-size:10px;color:var(--muted);margin-top:2px'>Reset: {reset_time_5h}</div></div>"
            else:
                limit_5h_html = "<span style='color:var(--muted);font-size:11px'>N/A</span>"
            
            # Week limit
            limit_week = r.get('limit_week')
            if limit_week:
                limit_week_pct = limit_week.get('percent', 0)
                bar_class_week = 'danger' if limit_week_pct > 90 else 'warn' if limit_week_pct > 70 else ''
                reset_at_week = limit_week.get('reset_at', 0)
                reset_time_week = datetime.fromtimestamp(reset_at_week).strftime('%m-%d %H:%M') if reset_at_week else 'N/A'
                limit_week_html = f"<div style='min-width:140px'><div style='font-family:DM Mono,monospace;font-size:11px;color:var(--muted)'>{limit_week_pct:.0f}%</div><div class='usage-bar'><div class='usage-fill {bar_class_week}' style='width:{min(limit_week_pct,100)}%'></div></div><div style='font-size:10px;color:var(--muted);margin-top:2px'>Reset: {reset_time_week}</div></div>"
            else:
                limit_week_html = "<span style='color:var(--muted);font-size:11px'>N/A</span>"
            
            # Plan type
            plan_type = r.get("plan_type", "unknown")
            plan_badge_class = "team" if plan_type == "team" else "free" if plan_type == "free" else ""
            plan_html = f"<span class=\"status-badge {plan_badge_class}\" style=\"font-size:10px\">{plan_type}</span>"

            html += f"<tr class='{r['status']}'><td>{i+1}</td><td style='font-family:DM Mono,monospace;font-size:12px'>{r['file']}</td><td>{plan_html}</td><td><span class='status-badge {r['status']}'>{r['status'].title()}</span></td><td>{limit_5h_html}</td><td>{limit_week_html}</td></tr>"
        
        html += """
        </tbody>
      </table>
    </div>
  </div>
</div>
<script>
function switchPool(pool) {
  document.querySelectorAll('.pool-btn').forEach(btn => {
    btn.classList.remove('active');
  });
  document.querySelector('.pool-btn.' + pool).classList.add('active');
  document.querySelectorAll('.pool-section').forEach(section => {
    section.classList.remove('active');
  });
  document.getElementById(pool + '-section').classList.add('active');
}
</script>
</body></html>"""
        
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_models(self):
        """返回支持的模型列表（OpenAI 和 Anthropic 格式兼容）"""
        models = {
            "object": "list",
            "data": [
                {
                    "id": "gpt-5.4",
                    "object": "model",
                    "created": 1677610602,
                    "owned_by": "openai"
                },
                {
                    "id": "gpt-5.4-xhigh",
                    "object": "model",
                    "created": 1677610602,
                    "owned_by": "openai"
                },
                {
                    "id": "gpt-5.3",
                    "object": "model",
                    "created": 1677610602,
                    "owned_by": "openai"
                },
                {
                    "id": "gpt-5.2",
                    "object": "model",
                    "created": 1677610602,
                    "owned_by": "openai"
                },
                {
                    "id": "gpt-5.2-codex",
                    "object": "model",
                    "created": 1677610602,
                    "owned_by": "openai"
                },
                {
                    "id": "claude-opus-4-6",
                    "object": "model",
                    "created": 1677610602,
                    "owned_by": "anthropic"
                },
                {
                    "id": "claude-sonnet-4-6",
                    "object": "model",
                    "created": 1677610602,
                    "owned_by": "anthropic"
                },
                {
                    "id": "claude-haiku-4-5",
                    "object": "model",
                    "created": 1677610602,
                    "owned_by": "anthropic"
                }
            ]
        }
        self._json(models)

    def _send_error(self, code, msg):
        body = json.dumps({"error": {"message": msg, "type": "proxy_error"}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_chat_completions(self, body: bytes, api_key: str, pool: str = "free"):
        """处理 Chat Completions 请求，转换成 Responses API 格式"""
        try:
            req_data = json.loads(body.decode())
        except Exception:
            self._send_error(400, "invalid json")
            return

        # 提取参数
        messages = req_data.get("messages", [])
        model = req_data.get("model", "gpt-5.4")
        stream = req_data.get("stream", False)

        # 构造 Responses API 请求
        # input 需要是消息列表，instructions 是字符串指令
        responses_req = {
            "model": model,
            "input": messages,  # 直接传递消息列表
            "instructions": "",  # 可选的额外指令
            "store": False,
            "stream": True  # ChatGPT 强制要求流式
        }

        proxy_handler = urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
        opener = urllib.request.build_opener(proxy_handler)

        for attempt in range(5):
            acc = get_next_account(pool)
            if not acc:
                self._send_error(503, "no accounts available")
                return

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {acc['access_token']}",
                "ChatGPT-Account-Id": acc["account_id"],
                "User-Agent": "codexs/0.3",
                "Accept": "text/event-stream",
            }

            req_body = json.dumps(responses_req).encode()
            req = urllib.request.Request(
                "https://chatgpt.com/backend-api/codex/responses",
                data=req_body,
                headers=headers,
                method="POST"
            )

            try:
                resp = opener.open(req, timeout=60)

                # 读取 SSE 流并转换成 Chat Completions 格式
                if stream:
                    self._stream_responses_to_chat(resp, api_key, model, acc)
                else:
                    self._convert_responses_to_chat(resp, api_key, model, acc)
                return

            except urllib.error.HTTPError as e:
                data = e.read()
                try:
                    err_json = json.loads(data.decode())
                    err_type = err_json.get("error", {}).get("type", "")
                    err_code = err_json.get("error", {}).get("code", "")
                    if err_type == "insufficient_quota" or err_code == "insufficient_quota" or e.code == 429:
                        mark_exhausted(acc["file"])
                        print(f"  -> quota exhausted, retrying (attempt {attempt+1})")
                        continue
                except Exception:
                    pass
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception as e:
                self._send_error(502, str(e))
                return

        self._send_error(503, "all retried accounts exhausted")

    def _stream_responses_to_chat(self, resp, api_key: str, model: str, acc: dict):
        """流式：Responses API SSE → Chat Completions SSE"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        response_id = f"chatcmpl-{int(time.time())}"
        input_tokens = 0
        output_tokens = 0
        full_text = ""

        for line in resp:
            line = line.decode('utf-8').strip()
            if not line or not line.startswith("data:"):
                continue

            data_str = line[5:].strip()
            if data_str == "[DONE]":
                # 发送最终 usage chunk
                usage_chunk = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [],
                    "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}
                }
                self.wfile.write(f"data: {json.dumps(usage_chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                break

            try:
                event = json.loads(data_str)
                event_type = event.get("type")

                if event_type == "response.output_text.delta":
                    delta_text = event.get("delta", "")
                    full_text += delta_text
                    chunk = {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}]
                    }
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())

                elif event_type == "response.completed":
                    usage_data = event.get("response", {}).get("usage", {})
                    input_tokens = usage_data.get("input_tokens", 0)
                    output_tokens = usage_data.get("output_tokens", 0)
                    # 记录用量
                    record_usage(api_key, model, input_tokens, output_tokens)
                    print(f"  -> OK stream key={api_key[:16]}... in={input_tokens} out={output_tokens} model={model}")

                    # 发送 finish chunk
                    finish_chunk = {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                    }
                    self.wfile.write(f"data: {json.dumps(finish_chunk)}\n\n".encode())
            except Exception:
                pass

    def _convert_responses_to_chat(self, resp, api_key: str, model: str, acc: dict):
        """非流式：Responses API SSE → Chat Completions JSON"""
        full_text = ""
        input_tokens = 0
        output_tokens = 0

        for line in resp:
            line = line.decode('utf-8').strip()
            if not line or not line.startswith("data:"):
                continue

            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break

            try:
                event = json.loads(data_str)
                event_type = event.get("type")

                if event_type == "response.output_text.delta":
                    full_text += event.get("delta", "")

                elif event_type == "response.completed":
                    usage_data = event.get("response", {}).get("usage", {})
                    input_tokens = usage_data.get("input_tokens", 0)
                    output_tokens = usage_data.get("output_tokens", 0)
            except Exception:
                pass

        # 记录用量
        record_usage(api_key, model, input_tokens, output_tokens)
        print(f"  -> OK non-stream key={api_key[:16]}... in={input_tokens} out={output_tokens} model={model}")

        # 返回 Chat Completions 格式
        response = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": full_text},
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens
            }
        }

        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_anthropic_messages(self, body: bytes, api_key: str, pool: str = "free"):
        """处理 Anthropic Messages API 请求，转换成 Responses API 格式"""
        try:
            req_data = json.loads(body.decode())
        except Exception:
            self._send_error(400, "invalid json")
            return

        # 提取参数
        messages = req_data.get("messages", [])
        model = req_data.get("model", "claude-sonnet-4-6")
        stream = req_data.get("stream", False)

        # 构造 Responses API 请求
        # 使用用户请求的模型（如果是非GPT模型，默认使用gpt-5.4）
        gpt_model = model if model.startswith("gpt-") else "gpt-5.4"
        responses_req = {
            "model": gpt_model,
            "input": messages,
            "instructions": "",
            "store": False,
            "stream": True
        }

        proxy_handler = urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
        opener = urllib.request.build_opener(proxy_handler)

        for attempt in range(5):
            acc = get_next_account(pool)
            if not acc:
                self._send_error(503, "no accounts available")
                return

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {acc['access_token']}",
                "ChatGPT-Account-Id": acc["account_id"],
                "User-Agent": "codexs/0.3",
                "Accept": "text/event-stream",
            }

            req_body = json.dumps(responses_req).encode()
            req = urllib.request.Request(
                "https://chatgpt.com/backend-api/codex/responses",
                data=req_body,
                headers=headers,
                method="POST"
            )

            try:
                resp = opener.open(req, timeout=60)

                # 转换响应格式
                if stream:
                    self._stream_responses_to_anthropic(resp, api_key, model, acc)
                else:
                    self._convert_responses_to_anthropic(resp, api_key, model, acc)
                return

            except urllib.error.HTTPError as e:
                data = e.read()
                try:
                    err_json = json.loads(data.decode())
                    err_type = err_json.get("error", {}).get("type", "")
                    err_code = err_json.get("error", {}).get("code", "")
                    if err_type == "insufficient_quota" or err_code == "insufficient_quota" or e.code == 429:
                        mark_exhausted(acc["file"])
                        print(f"  -> quota exhausted, retrying (attempt {attempt+1})")
                        continue
                except Exception:
                    pass
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception as e:
                self._send_error(502, str(e))
                return

        self._send_error(503, "all retried accounts exhausted")

    def _stream_responses_to_anthropic(self, resp, api_key: str, model: str, acc: dict):
        """流式：Responses API SSE → Anthropic Messages API SSE"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        message_id = f"msg_{int(time.time())}"
        input_tokens = 0
        output_tokens = 0
        content_block_index = 0

        # 发送 message_start 事件
        message_start = {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}
            }
        }
        self.wfile.write(f"event: message_start\ndata: {json.dumps(message_start)}\n\n".encode())

        # 发送 content_block_start 事件
        content_block_start = {
            "type": "content_block_start",
            "index": content_block_index,
            "content_block": {"type": "text", "text": ""}
        }
        self.wfile.write(f"event: content_block_start\ndata: {json.dumps(content_block_start)}\n\n".encode())

        for line in resp:
            line = line.decode('utf-8').strip()
            if not line or not line.startswith("data:"):
                continue

            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break

            try:
                event = json.loads(data_str)
                event_type = event.get("type")

                if event_type == "response.output_text.delta":
                    delta_text = event.get("delta", "")
                    # 发送 content_block_delta 事件
                    delta_event = {
                        "type": "content_block_delta",
                        "index": content_block_index,
                        "delta": {"type": "text_delta", "text": delta_text}
                    }
                    self.wfile.write(f"event: content_block_delta\ndata: {json.dumps(delta_event)}\n\n".encode())

                elif event_type == "response.completed":
                    usage_data = event.get("response", {}).get("usage", {})
                    input_tokens = usage_data.get("input_tokens", 0)
                    output_tokens = usage_data.get("output_tokens", 0)

                    # 记录用量
                    record_usage(api_key, model, input_tokens, output_tokens)
                    print(f"  -> OK stream key={api_key[:16]}... in={input_tokens} out={output_tokens} model={model}")

                    # 发送 content_block_stop 事件
                    content_block_stop = {
                        "type": "content_block_stop",
                        "index": content_block_index
                    }
                    self.wfile.write(f"event: content_block_stop\ndata: {json.dumps(content_block_stop)}\n\n".encode())

                    # 发送 message_delta 事件
                    message_delta = {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": output_tokens}
                    }
                    self.wfile.write(f"event: message_delta\ndata: {json.dumps(message_delta)}\n\n".encode())

                    # 发送 message_stop 事件
                    message_stop = {"type": "message_stop"}
                    self.wfile.write(f"event: message_stop\ndata: {json.dumps(message_stop)}\n\n".encode())
            except Exception:
                pass

    def _convert_responses_to_anthropic(self, resp, api_key: str, model: str, acc: dict):
        """非流式：Responses API SSE → Anthropic Messages API JSON"""
        full_text = ""
        input_tokens = 0
        output_tokens = 0

        for line in resp:
            line = line.decode('utf-8').strip()
            if not line or not line.startswith("data:"):
                continue

            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break

            try:
                event = json.loads(data_str)
                event_type = event.get("type")

                if event_type == "response.output_text.delta":
                    full_text += event.get("delta", "")

                elif event_type == "response.completed":
                    usage_data = event.get("response", {}).get("usage", {})
                    input_tokens = usage_data.get("input_tokens", 0)
                    output_tokens = usage_data.get("output_tokens", 0)
            except Exception:
                pass

        # 记录用量
        record_usage(api_key, model, input_tokens, output_tokens)
        print(f"  -> OK non-stream key={api_key[:16]}... in={input_tokens} out={output_tokens} model={model}")

        # 返回 Anthropic Messages API 格式
        response = {
            "id": f"msg_{int(time.time())}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": full_text}],
            "model": model,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens
            }
        }

        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    load_accounts()
    load_keys()
    server = HTTPServer(("0.0.0.0", 8765), ProxyHandler)
    print(f"Proxy listening on :8765")
    server.serve_forever()