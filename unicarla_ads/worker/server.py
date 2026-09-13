"""ADS Worker 的最小 HTTP 服务。"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def _create_adapter():
    ads_name = os.environ.get("UNICARLA_ADS_NAME", "interfuser")
    if ads_name == "interfuser":
        from .interfuser_adapter import InterFuserAdapter

        return InterFuserAdapter()
    if ads_name == "lead":
        from .lead_adapter import LeadAdapter

        return LeadAdapter()
    if ads_name == "autoware":
        from .autoware_adapter import AutowareAdapter

        return AutowareAdapter()
    raise RuntimeError("不支持的 ADS: {}".format(ads_name))


ADAPTER = _create_adapter()

# cudnn 的基准缓存是线程本地的；HTTP 每请求一线程会导致每次 step
# 都重新做一遍逐形状基准搜索（数十秒级）。所有 ADAPTER 调用固定在
# 这个常驻线程上执行，保证模型状态和 cudnn 缓存只初始化一次。
EXECUTOR = ThreadPoolExecutor(max_workers=1)


class RequestHandler(BaseHTTPRequestHandler):
    """处理单实例 ADS 接口。"""

    def do_GET(self):
        if self.path == "/health":
            self._write_json(200, {"ok": True})
        elif self.path == "/status":
            self._write_json(200, EXECUTOR.submit(ADAPTER.status).result())
        else:
            self._write_json(404, {"error": "接口不存在"})

    def do_POST(self):
        try:
            payload = self._read_json()
            if self.path == "/initialize":
                result = EXECUTOR.submit(
                    ADAPTER.initialize,
                    agent_config=payload.get("agent_config"),
                    agent_path=payload.get("agent_path"),
                ).result()
            elif self.path == "/deploy":
                result = EXECUTOR.submit(ADAPTER.deploy, payload).result()
            elif self.path == "/step":
                result = EXECUTOR.submit(
                    ADAPTER.step,
                    payload["frame_id"],
                    timeout=payload.get("timeout"),
                ).result()
            elif self.path == "/download_gui":
                if not hasattr(ADAPTER, "render_gui_video"):
                    raise RuntimeError("当前 ADS 不支持 GUI 视频下载")
                video = EXECUTOR.submit(
                    ADAPTER.render_gui_video,
                    payload.get("fps", 20.0),
                ).result()
                self._write_binary(200, video, "video/mp4")
                return
            elif self.path == "/close":
                result = EXECUTOR.submit(ADAPTER.close).result()
            else:
                self._write_json(404, {"error": "接口不存在"})
                return
            self._write_json(200, result)
        except (KeyError, TypeError, ValueError) as exc:
            self._write_json(400, {"error": str(exc)})
        except RuntimeError as exc:
            self._write_json(409, {"error": str(exc)})
        except Exception as exc:
            self._write_json(500, {"error": str(exc)})

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _write_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_binary(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        print("[HTTP] " + format_string % args)


def main():
    port = int(os.environ.get("UNICARLA_ADS_PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), RequestHandler)
    print("ADS Worker listening on 0.0.0.0:{}".format(port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ADAPTER.close()
        server.server_close()


if __name__ == "__main__":
    main()
