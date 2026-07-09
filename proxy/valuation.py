#!/usr/bin/env python3
"""本机蛋卷估值代理 — 解决 file:// / localhost 打开 HTML 时的跨域限制。

用法（在项目根目录）：
    python3 proxy/valuation.py

然后双击打开 index.html，估值会走蛋卷实时数据（横幅显示 ·蛋卷）。
按 Ctrl+C 停止。
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import URLError
from urllib.request import Request, urlopen

PORT = 8787
UPSTREAMS = [
    "https://danjuanfunds.com/djapi/index_eva/dj",
    "https://danjuanapp.com/djapi/index_eva/dj",
]
UPSTREAM_HEADERS = {
    "Referer": "https://danjuanfunds.com/djmodule/value-center",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in CORS.items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/", "/valuation"):
            self.send_error(404)
            return

        for url in UPSTREAMS:
            try:
                req = Request(url, headers=UPSTREAM_HEADERS)
                with urlopen(req, timeout=15) as resp:
                    body = resp.read()
                self.send_response(200)
                for k, v in CORS.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                return
            except URLError as e:
                print(f"  upstream fail: {url} ({e})")

        self.send_response(502)
        for k, v in CORS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":"upstream unavailable"}')

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {args[0]}")


def main():
    addr = ("0.0.0.0", PORT)
    print(f"蛋卷估值代理  http://127.0.0.1:{PORT}/  （局域网可访问 :{PORT}）")
    print("保持运行，浏览器打开 magic 页面")
    HTTPServer(addr, Handler).serve_forever()


if __name__ == "__main__":
    main()
