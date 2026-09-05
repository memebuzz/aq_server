#!/usr/bin/env python3
"""AquesTalk10 + AqKanji2Koe 最小ローカルサーバー（Chrome拡張用）.

拡張（memebuzz_chrome_extension/src/lib/aqserver.ts）はこのサーバーにだけ接続します:
  GET  /version  -> {"engine": "aquesTalk10", "evalMode": bool, "kanji": bool, "voices": [...]}
  POST /synth {"text": "...", "speed": 50-300, "voice": "reimu"|"marisa"|...} -> WAVバイナリ

起動:
  git clone https://github.com/memebuzz/aq_server
  cd aq_server
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
  優先順位: 引数 > 環境変数 > .env ファイル
  (.env はカレントか本スクリプト隣に置くか --env-file で指定。詳細は .env.example 参照)
  AQ_DEV_KEY / AQ_USR_KEY / AQ_KANJI_DEV_KEY のいずれかで指定してください。
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
import re
import struct
import sys
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def load_dotenv(env_file: str = "") -> str | None:
    """最小 .env ローダー（標準ライブラリのみ・pip不要）.

    - 既に設定済みの環境変数は上書きしない（優先順位: 引数 > 環境変数 > .env）
    - `KEY=VALUE` 形式。空行・`#` コメント・`export ` 接頭辞に対応。
      値の前後クォート（'/"）を外し、非クォート値の ` #` 以降はコメント扱い。
    - 探索順: --env-file 指定 > カレントの .env > 本スクリプト隣の .env
    - 戻り値は読み込んだファイルパス（無ければ None）。
    """
    candidates = (
        [env_file]
        if env_file
        else [
            os.path.join(os.getcwd(), ".env"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        ]
    )
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("export "):
                        line = line[len("export ") :].lstrip()
                    if "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    if not key or key[0].isdigit() or not key.replace("_", "").isalnum():
                        continue
                    value = value.strip()
                    if value[:1] in ("'", '"'):
                        # 先頭クォート〜対応する閉じクォートまでを値とする（以降はコメント扱い）
                        quote = value[0]
                        chars: list[str] = []
                        i = 1
                        while i < len(value):
                            c = value[i]
                            if quote == '"' and c == "\\" and i + 1 < len(value):
                                nxt = value[i + 1]
                                chars.append({"n": "\n", "t": "\t"}.get(nxt, nxt))
                                i += 2
                                continue
                            if c == quote:
                                break
                            chars.append(c)
                            i += 1
                        value = "".join(chars)
                    else:
                        # 非クォート値の末尾コメント（" #"）を除去
                        hash_idx = value.find(" #")
                        if hash_idx != -1:
                            value = value[:hash_idx].rstrip()
                    if key in os.environ:
                        continue
                    os.environ[key] = value
        except OSError as e:
            print(f"[aq_server] .env 読み込み失敗 ({path}): {e}", flush=True)
            return None
        return path
    return None


class AQTKVoice(ctypes.Structure):
    """AquesTalk10 の声質パラメータ（全int・28バイト）."""

    _fields_ = [
        ("bas", ctypes.c_int),  # 基本素片 F1E/F2E/M1E (0/1/2)
        ("spd", ctypes.c_int),  # 話速 50-300
        ("vol", ctypes.c_int),  # 音量 0-300
        ("pit", ctypes.c_int),  # 高さ 20-200
        ("acc", ctypes.c_int),  # アクセント 0-200
        ("lmd", ctypes.c_int),  # 音程1 0-200
        ("fsc", ctypes.c_int),  # 音程2 50-200
    ]


# ゆっくり実況向けの声質プリセット一覧
# AquesTalk10 公式プリセット（AqTk10App/MYukkuriVoice準拠）に基づくパラメータ
VOICE_PRESETS: dict[str, dict[str, int | str]] = {
    "reimu": {
        "id": "reimu",
        "name": "ゆっくり霊夢（女声1）",
        "bas": 0,  # F1E
        "vol": 100,
        "pit": 100,
        "acc": 100,
        "lmd": 100,
        "fsc": 100,
    },
    "marisa": {
        "id": "marisa",
        "name": "ゆっくり魔理沙（女声2）",
        "bas": 1,  # F2E
        "vol": 100,
        "pit": 77,
        "acc": 150,
        "lmd": 100,
        "fsc": 100,
    },
    "yukkuri_f3": {
        "id": "yukkuri_f3",
        "name": "女声3（高音・F3）",
        "bas": 0,  # F1E
        "vol": 100,
        "pit": 100,
        "acc": 100,
        "lmd": 61,
        "fsc": 148,
    },
    "yukkuri_m1": {
        "id": "yukkuri_m1",
        "name": "男声1（M1）",
        "bas": 2,  # M1E
        "vol": 100,
        "pit": 30,
        "acc": 100,
        "lmd": 100,
        "fsc": 100,
    },
    "yukkuri_r1": {
        "id": "yukkuri_r1",
        "name": "ロボット1（R1）",
        "bas": 2,  # M1E
        "vol": 100,
        "pit": 30,
        "acc": 20,
        "lmd": 190,
        "fsc": 100,
    },
}

DEFAULT_VOICE_ID = "reimu"


def resolve_voice_params(voice_arg: str | dict | None) -> dict[str, int]:
    """プリセット名または辞書から AQTKVoice 用パラメータを解決する."""
    base = dict(VOICE_PRESETS[DEFAULT_VOICE_ID])
    if isinstance(voice_arg, str) and voice_arg in VOICE_PRESETS:
        base = dict(VOICE_PRESETS[voice_arg])
    elif isinstance(voice_arg, dict):
        preset_id = voice_arg.get("id") or voice_arg.get("preset")
        if isinstance(preset_id, str) and preset_id in VOICE_PRESETS:
            base = dict(VOICE_PRESETS[preset_id])
        for k in ("bas", "vol", "pit", "acc", "lmd", "fsc"):
            if k in voice_arg and isinstance(voice_arg[k], (int, float)):
                base[k] = int(voice_arg[k])
    return {k: int(base[k]) for k in ("bas", "vol", "pit", "acc", "lmd", "fsc")}



# AquesTalk10 公式マニュアルのエラーコード表（size に返る値）
AQ_SYNTH_ERRORS = {
    100: "その他のエラー",
    101: "メモリ不足",
    103: "音声記号列指定エラー（語頭の長音・促音の連続など）",
    104: "音声記号列に有効な読みがない",
    105: "未定義の読み記号（漢字や記号混じり。辞書なし時は漢字不可）",
    106: "タグの指定が正しくない",
    107: "タグの長さが制限を超過",
    108: "タグ内の値の指定が正しくない",
    120: "音声記号列が長すぎる（短く分割してください）",
    121: "1フレーズ中の読み記号が多すぎる（短く分割してください）",
    122: "音声記号列が長い（短く分割してください）",
}

# AqKanji2Koe が変換しきれなかった入力を検出するための範囲。
# AquesTalk の音声記号列に漢字が残ると code=105 になる。
KANJI_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class Engine:
    """実ライブラリのハンドル保持。ロード失敗時は mock/missing として動作."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.mock = args.mock
        # 相対パスはまず起動ディレクトリを尊重し、見つからない場合は
        # aq_server.py の隣を基準にする。リポジトリルートから起動しても辞書を読めるようにする。
        dic_dir = args.dic_dir
        if dic_dir and not os.path.exists(dic_dir):
            script_relative_dic = os.path.join(os.path.dirname(os.path.abspath(__file__)), dic_dir)
            if os.path.exists(script_relative_dic):
                dic_dir = script_relative_dic
        self.dic_dir = dic_dir
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
                    create = self.kanji_lib.AqKanji2Koe_Create
                    # 注意: 第2引数はエラーコード出力用の int*（NULL不可）。
                    # 0 を渡すとネイティブ側の書き込みで segfault する。
                    create.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
                    create.restype = ctypes.c_void_p
                    dic = self.dic_dir.encode("utf-8") if self.dic_dir else b""
                    kanji_err = ctypes.c_int(0)
                    self.kanji_handle = create(dic, ctypes.byref(kanji_err))
                    if not self.kanji_handle:
                        # 例: code=200 は辞書読込失敗（aqdic.bin 等なし）、101 は辞書パス不正
                        raise RuntimeError(
                            f"辞書の読み込みに失敗しました（code={kanji_err.value}）。"
                            "aq_dic/ に aqdic.bin・aq_user.dic を配置してください"
                        )
                    convert = self.kanji_lib.AqKanji2Koe_Convert
                    convert.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
                    convert.restype = ctypes.c_int
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
        voices = [{"id": v["id"], "name": v["name"]} for v in VOICE_PRESETS.values()]
        base_payload = {
            "engine": "aquesTalk10",
            "evalMode": self.eval_mode,
            "kanji": self.kanji_available,
            "voices": voices,
            "defaultVoice": DEFAULT_VOICE_ID,
        }
        if self.mock:
            base_payload["mock"] = True
            return base_payload
        if self.aq_lib is None:
            return {
                "engine": "not_initialized",
                "evalMode": True,
                "kanji": False,
                "voices": voices,
                "defaultVoice": DEFAULT_VOICE_ID,
                "error": self.load_error or "エンジン未初期化",
            }
        if self.load_error:
            base_payload["error"] = self.load_error
        return base_payload

    # -- 合成 --
    def synth(self, text: str, speed: int, voice: str | dict | None = None) -> bytes:
        speed = max(50, min(300, int(speed or 100)))
        params = resolve_voice_params(voice)
        if self.mock or self.aq_lib is None:
            if self.aq_lib is None and not self.mock:
                raise RuntimeError(self.load_error or "エンジン未初期化")
            return mock_wav(text, speed)
        koe = self.to_koe(text)
        if KANJI_RE.search(koe):
            raise RuntimeError(
                "漢字を読みへ変換できませんでした。AqKanji2Koeの辞書（aq_dic/aqdic.bin）を確認してください"
            )
        synth_fn = getattr(self.aq_lib, "AquesTalk_Synthe_Utf8", None) or getattr(
            self.aq_lib, "AquesTalk_Synth", None
        )
        # 版により FreeWave / FreeWav の表記揺れがある
        free_fn = getattr(self.aq_lib, "AquesTalk_FreeWave", None) or getattr(
            self.aq_lib, "AquesTalk_FreeWav", None
        )
        if synth_fn is None or free_fn is None:
            raise RuntimeError("音声合成関数が見つかりません（版を確認してください）")
        free_fn.argtypes = [ctypes.c_void_p]
        free_fn.restype = None
        size = ctypes.c_int(0)
        if getattr(synth_fn, "__name__", "") == "AquesTalk_Synth":
            # 旧版API: Synth(koe, speed, size)
            synth_fn.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
            synth_fn.restype = ctypes.c_void_p
            ptr = synth_fn(koe.encode("utf-8"), ctypes.c_int(speed), ctypes.byref(size))
        else:
            # AquesTalk10: Synthe_Utf8(voice, koe, size)
            synth_fn.argtypes = [
                ctypes.POINTER(AQTKVoice),
                ctypes.c_char_p,
                ctypes.POINTER(ctypes.c_int),
            ]
            synth_fn.restype = ctypes.c_void_p
            voice_obj = AQTKVoice(spd=speed, **params)
            ptr = synth_fn(ctypes.byref(voice_obj), koe.encode("utf-8"), ctypes.byref(size))
        if not ptr or size.value <= 0:
            code = size.value if size.value > 0 else 0
            reason = AQ_SYNTH_ERRORS.get(code, "記号列を確認してください")
            raise RuntimeError(f"音声合成に失敗しました（code={code}: {reason}）")
        try:
            return ctypes.string_at(ptr, size.value)
        finally:
            try:
                free_fn(ctypes.c_void_p(ptr))
            except Exception:  # noqa: BLE001
                pass

    def to_koe(self, text: str) -> str:
        """漢字混じり文を音声記号列へ変換する。かな文はそのまま返す."""
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
            # AqKanji2Koe_Convert は成功時に戻り値 0 を返し、読み列は
            # 出力バッファへ書き込む。戻り値だけで成功判定してはいけない。
            if buf.value:
                return buf.value.decode("utf-8", errors="replace")
            if n:
                raise RuntimeError(f"変換エラー（code={n}）")
        except Exception as e:  # noqa: BLE001 - HTTP経由で原因を返す
            raise RuntimeError(f"AqKanji2Koeの漢字変換に失敗しました: {e}") from e
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
        voice = data.get("voice") or data.get("preset") or DEFAULT_VOICE_ID
        try:
            wav = self.engine.synth(text, int(data.get("speed", 100)), voice)
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
    p.add_argument(
        "--env-file",
        default="",
        help=".env のパス（未指定時はカレント・スクリプト隣の .env を自動探索）",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env_path = load_dotenv(args.env_file)
    Handler.engine = Engine(args)
    payload = Handler.engine.version_payload()
    mode = "mock" if args.mock else ("real" if Handler.engine.aq_lib else "missing-lib")
    print(f"[aq_server] http://{args.host}:{args.port} mode={mode} {payload}", flush=True)
    if env_path:
        print(f"[aq_server] .env 読み込み: {env_path}", flush=True)
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
