#!/usr/bin/env python3
"""AquesTalk10 + AqKanji2Koe 最小ローカルサーバー（Chrome拡張用）.

拡張（memebuzz_chrome_extension/src/lib/aqserver.ts）はこのサーバーにだけ接続します:
  GET  /version  -> {"engine": "aquesTalk10", "evalMode": bool, "kanji": bool}
  POST /synth {"text": "...", "speed": 50-300} -> WAVバイナリ

起動:
  cd projects/aq_server
  python3 aq_server.py --mock --port 50082      # ライブラリ無しでの動作確認用
  python3 aq_server.py --port 50082              # 実ライブラリ使用
  curl http://127.0.0.1:50082/version

実ライブラリを使う場合（別途アクエストから取得・配置が必要）:
  aq_server/
    lib/libAquesTalk10.dylib(.so)   # 音声合成ライブラリ
    lib/libAqKanji2Koe.dylib(.so)   # 言語処理ライブラリ
    aq_dic/                         # AqKanji2Koe 辞書
  python3 aq_server.py --port 50082 \
    --aquestalk-lib ./lib/libAquesTalk10.dylib \
    --kanji-lib ./lib/libAqKanji2Koe.dylib \
    --dic-dir ./aq_dic

ライセンス:
  開発キー未設定でも起動しますが評価版動作（ナ行・マ行→ヌ）です。
  AQ_DEV_KEY / AQ_USR_KEY / AQ_KANJI_DEV_KEY 環境変数か引数で指定してください。
  製品利用には開発・使用／頒布ライセンスが必要です（詳細はアクエストのサイト参照）。

依存: Python標準ライブラリのみ（pip不要）。
"""

from __future__ import annotations

import argparse
import ctypes
import io
import json
import math
import os
import struct
import sys
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Engine:
    """実ライブラリのハンドル保持。ロード失敗時は mock/missing として動作."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.mock = args.mock
        self.dic_dir = args.dic_dir
        self.dev_key = args.dev_key or os.environ.get("AQ_DEV_KEY", "")
        self.usr_key = args.usr_key or os.environ.get("AQ_USR_KEY", "")
        self.kanji_dev_key = args.kanji_dev_key or os.environ.get("AQ_KANJI_DEV_KEY", "")

        self.aq_lib = None
        self.kanji_lib = None
        self.kanji_handle = None
        self.load_error: str | None = None

        if self.mock:
            return
        # --- AquesTalk10 ロード ---
        aq_candidates = [p for p in [args.aquestalk_lib] if p] + [
            "./lib/libAquesTalk10.dylib",
            "./lib/libAquesTalk10.so",
            "libAquesTalk10.dylib",
            "libAquesTalk10.so",
        ]
        for p in aq_candidates:
            try:
                if os.path.exists(p):
                    self.aq_lib = ctypes.CDLL(p)
                    break
            except OSError:
                continue
        if self.aq_lib is None:
            self.load_error = (
                "AquesTalk10ライブラリが見つかりません。--mock で動作確認するか、"
                "--aquestalk-lib でパスを指定してください。"
            )
            return
        try:
            if self.dev_key and hasattr(self.aq_lib, "AquesTalk_SetDevKey"):
                self.aq_lib.AquesTalk_SetDevKey(self.dev_key.encode("utf-8"))
            if self.usr_key and hasattr(self.aq_lib, "AquesTalk_SetUsrKey"):
                self.aq_lib.AquesTalk_SetUsrKey(self.usr_key.encode("utf-8"))
        except Exception as e:  # noqa: BLE001 - 起動は継続しエラーを/versionで返す
            self.load_error = f"ライセンスキー設定に失敗: {e}"

        # --- AqKanji2Koe ロード（無くても動作可: かな直接入力扱い） ---
        kanji_candidates = [p for p in [args.kanji_lib] if p] + [
            "./lib/libAqKanji2Koe.dylib",
            "./lib/libAqKanji2Koe.so",
            "libAqKanji2Koe.dylib",
            "libAqKanji2Koe.so",
        ]
        for p in kanji_candidates:
            try:
                if os.path.exists(p):
                    self.kanji_lib = ctypes.CDLL(p)
                    break
            except OSError:
                continue
        if self.kanji_lib is not None:
            try:
                if self.kanji_dev_key and hasattr(self.kanji_lib, "AqKanji2Koe_SetDevKey"):
                    self.kanji_lib.AqKanji2Koe_SetDevKey(self.kanji_dev_key.encode("utf-8"))
                if hasattr(self.kanji_lib, "AqKanji2Koe_Create"):
                    self.kanji_lib.AqKanji2Koe_Create.restype = ctypes.c_void_p
                    dic = self.dic_dir.encode("utf-8") if self.dic_dir else b""
                    try:
                        self.kanji_handle = self.kanji_lib.AqKanji2Koe_Create(dic, 0)
                    except Exception:
                        # 版により引数が違うため辞書パスのみで再試行
                        self.kanji_handle = self.kanji_lib.AqKanji2Koe_Create(dic)
            except Exception as e:  # noqa: BLE001
                self.load_error = f"AqKanji2Koe初期化に失敗: {e}"
                self.kanji_lib = None
                self.kanji_handle = None

    @property
    def kanji_available(self) -> bool:
        return self.mock or (self.kanji_lib is not None and self.kanji_handle is not None)

    @property
    def eval_mode(self) -> bool:
        if self.mock:
            return True
        return not bool(self.dev_key)

    def version_payload(self) -> dict:
        if self.mock:
            return {"engine": "aquesTalk10", "evalMode": True, "kanji": True, "mock": True}
        if self.aq_lib is None:
            return {
                "engine": "not_initialized",
                "evalMode": True,
                "kanji": False,
                "error": self.load_error or "エンジン未初期化",
            }
        payload = {"engine": "aquesTalk10", "evalMode": self.eval_mode, "kanji": self.kanji_available}
        if self.load_error:
            payload["error"] = self.load_error
        return payload

    # -- 合成 --
    def synth(self, text: str, speed: int) -> bytes:
        speed = max(50, min(300, int(speed or 100)))
        if self.mock or self.aq_lib is None:
            if self.aq_lib is None and not self.mock:
                raise RuntimeError(self.load_error or "エンジン未初期化")
            return mock_wav(text, speed)
        koe = self.to_koe(text)
        synth_fn = getattr(self.aq_lib, "AquesTalk_Synthe_Utf8", None) or getattr(
            self.aq_lib, "AquesTalk_Synth", None
        )
        free_fn = getattr(self.aq_lib, "AquesTalk_FreeWav", None)
        if synth_fn is None or free_fn is None:
            raise RuntimeError("AquesTalk_Synthe_Utf8 が見つかりません（版を確認してください）")
        size = ctypes.c_int(0)
        synth_fn.restype = ctypes.c_void_p
        ptr = synth_fn(koe.encode("utf-8"), ctypes.c_int(speed), ctypes.byref(size))
        if not ptr or size.value <= 0:
            raise RuntimeError("音声合成に失敗しました（記号列を確認してください）")
        try:
            return ctypes.string_at(ptr, size.value)
        finally:
            try:
                free_fn(ctypes.c_void_p(ptr))
            except Exception:  # noqa: BLE001
                pass

    def to_koe(self, text: str) -> str:
        """漢字混じり文→音声記号列。辞書無し時は入力をそのまま返す."""
        if self.kanji_lib is None or self.kanji_handle is None:
            return text
        try:
            convert = self.kanji_lib.AqKanji2Koe_Convert
            buf = ctypes.create_string_buffer(8192)
            n = convert(
                ctypes.c_void_p(self.kanji_handle),
                text.encode("utf-8"),
                buf,
                ctypes.c_int(ctypes.sizeof(buf)),
            )
            if n and buf.value:
                return buf.value.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - フォールバックで入力を返す
            pass
        return text


def mock_wav(text: str, speed: int, sample_rate: int = 8000) -> bytes:
    """ライセンス無しで拡張の配線確認をするためのダミーWAV（サイン波）."""
    dur = max(0.5, min(5.0, 0.5 + len(text.strip()) * 0.08)) * (100 / max(50, speed))
    n = int(sample_rate * dur)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        for i in range(n):
            # 440Hz + 始端終端フェードで「無音誤検出」を避ける
            fade = min(1.0, i / (sample_rate * 0.05), (n - i) / (sample_rate * 0.05))
            v = int(12000 * fade * math.sin(2 * math.pi * 440 * i / sample_rate))
            w.writeframes(struct.pack("<h", v))
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    engine: Engine  # main() で注入

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: D102
        sys.stderr.write(f"[aq_server] {self.address_string()} {fmt % args}\n")

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: D102
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: D102
        if self.path == "/version":
            self._json(self.engine.version_payload())
        elif self.path in ("/", "/health"):
            self._json({"ok": True, "usage": "GET /version, POST /synth"})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: D102
        if self.path != "/synth":
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"error": "JSONを解釈できません"}, 400)
            return
        text = str(data.get("text", "")).strip()
        if not text:
            self._json({"error": "text が空です"}, 400)
            return
        try:
            wav = self.engine.synth(text, int(data.get("speed", 100)))
        except (RuntimeError, ValueError, OSError) as e:
            self._json({"error": str(e)}, 500)
            return
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        self.end_headers()
        self.wfile.write(wav)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AquesTalk10最小ローカルサーバー（Chrome拡張用）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=50082)
    p.add_argument("--mock", action="store_true", help="実ライブラリ無しでダミー音声を返す")
    p.add_argument("--aquestalk-lib", default="")
    p.add_argument("--kanji-lib", default="")
    p.add_argument("--dic-dir", default="./aq_dic")
    p.add_argument("--dev-key", default="")
    p.add_argument("--usr-key", default="")
    p.add_argument("--kanji-dev-key", default="")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    Handler.engine = Engine(args)
    payload = Handler.engine.version_payload()
    mode = "mock" if args.mock else ("real" if Handler.engine.aq_lib else "missing-lib")
    print(f"[aq_server] http://{args.host}:{args.port} mode={mode} {payload}", flush=True)
    if mode == "missing-lib":
        print(f"[aq_server] {payload.get('error')} (--mock で配線確認できます)", flush=True)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
