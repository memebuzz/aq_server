#!/usr/bin/env python3
"""AquesTalk1/10 + AqKanji2Koe 最小ローカルサーバー（Chrome拡張用）.

拡張（memebuzz_chrome_extension/src/lib/aqserver.ts）はこのサーバーにだけ接続します:
  GET  /version  -> {"engine": "aquesTalk10"|"aquesTalk1", "engines": {...},
                     "evalMode": bool, "kanji": bool, "voices": [...]}
  POST /synth {"text": "...", "speed": 50-300, "voice": "reimu"|"f1"|...,
               "engine": "aquestalk10"|"aquestalk1"} -> WAVバイナリ

両エンジンは同居します（見つかったライブラリは両方ロード）。
`engine` 指定が無ければ --engine / AQ_ENGINE のデフォルトエンジンを使います。
Chrome拡張側の「1か10か」選択はこの `engine` フィールドで切り替えます。

起動:
  git clone https://github.com/memebuzz/aq_server
  cd aq_server
  python3 aq_server.py --mock --port 50082      # ライブラリ無しでの動作確認用
  python3 aq_server.py --port 50082              # 実ライブラリ使用（両方自動探索）
  curl http://127.0.0.1:50082/version

実ライブラリを使う場合（別途アクエストから取得・配置が必要）:
  aq_server/
    lib/libAquesTalk10.dylib(.so)    # AquesTalk10（単一ライブラリ・声質は構造体で指定）
    lib/libAquesTalk1-<voice>.dylib(.so)  # AquesTalk1（声ごとに別ライブラリ。
                                          #   例: libAquesTalk1-f1.dylib, -m1.dylib, -r1.dylib …。
                                          #   旧来の単一 libAquesTalk.dylib(.so) もあれば使います）
    lib/libAqKanji2Koe.dylib(.so)   # 言語処理ライブラリ
    aq_dic/                         # AqKanji2Koe 辞書
  python3 aq_server.py --port 50082 \
    --aquestalk-lib ./lib/libAquesTalk10.dylib \
    --aquestalk1-lib ./lib \
    --kanji-lib ./lib/libAqKanji2Koe.dylib \
    --dic-dir ./aq_dic

# MeCab+NEologdによる漢字読み精度向上（pip install mecab-python3 等が必要）:
  python3 aq_server.py --port 50082 \
    --aquestalk-lib ./lib/libAquesTalk10.dylib \
    --kanji-lib ./lib/libAqKanji2Koe.dylib \
    --dic-dir ./aq_dic \
    --mecab-dic /path/to/neologd

ライセンス:
  開発キー未設定の場合は評価版動作（ナ行・マ行→ヌ）になります。製品利用には開発・使用／頒布ライセンスが必要です。

依存: Python標準ライブラリのみ（pip不要）。
MeCab+NEologd使用時のみ `pip install mecab-python3` 等が必要。
"""

from __future__ import annotations

import argparse
import ctypes
import glob
import io
import json
import math
import os
import re
import struct
import sys
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal, Optional


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

# AquesTalk1 エラーコード。
# 実測では AquesTalk10 と同じ正のコード（100-122）が返る。
# 旧資料の負コードにも念のため対応する。
AQ1_SYNTH_ERRORS = {
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
    -1: "その他のエラー",
    -2: "メモリ不足",
    -3: "音声記号列指定エラー",
    -4: "音声記号列に有効な読みがない",
    -5: "未定義の読み記号",
    -6: "タグの指定が正しくない",
    -7: "タグの長さが制限を超過",
    -8: "タグ内の値の指定が正しくない",
    -9: "音声記号列が長すぎる",
}

# エンジン種別（リクエスト・設定では大文字小文字を区別しない）
EngineType = Literal["aquestalk10", "aquestalk1"]


def normalize_engine(value: object, default: EngineType = "aquestalk10") -> EngineType:
    """engine指定を正規化する。'1'/'10' の略記も受け付ける."""
    if not isinstance(value, str) or not value.strip():
        return default
    v = value.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    if v in ("1", "aquestalk1", "aqtalk1", "aq1"):
        return "aquestalk1"
    if v in ("10", "aquestalk10", "aqtalk10", "aq10"):
        return "aquestalk10"
    return default


# AquesTalk1 は声ごとに別ライブラリ（libAquesTalk1-<voice>.dylib）。
# ファイル接尾辞 -> (voice id, 表示名) の対応表。未知の接尾辞は "名称 (suffix)" で公開する。
AQ1_LIB_VOICE_NAMES: dict[str, str] = {
    "f1": "女性1 (F1)",
    "f2": "女性2 (F2)",
    "f3": "女性3 (F3)",
    "m1": "男性1 (M1)",
    "m2": "男性2 (M2)",
    "r1": "ロボット (R1)",
    "dvd": "DVD (dvd)",
    "jgr": "JGR (jgr)",
    "imd1": "IMD1 (imd1)",
}

# 後方互換エイリアス（旧プリセット名 -> lib接尾辞）
AQ1_VOICE_ALIASES: dict[str, str] = {
    "robot": "r1",
    "r2": "r1",
    "female1": "f1",
    "female2": "f2",
    "male1": "m1",
    "male2": "m2",
}


def normalize_aq1_voice(value: str | dict | None, available: list[str]) -> str:
    """AquesTalk1の声指定を、ロード済みライブラリのvoice idへ正規化する."""
    avail = available or ["f1"]
    raw: str = ""
    if isinstance(value, str):
        raw = value.strip().lower()
    elif isinstance(value, dict):
        preset = value.get("id") or value.get("preset") or value.get("voice")
        if isinstance(preset, str):
            raw = preset.strip().lower()
    if not raw:
        return avail[0] if "f1" not in avail else "f1"
    raw = AQ1_VOICE_ALIASES.get(raw, raw)
    if raw in avail:
        return raw
    # "reimu" など Aq10 名が来たら f1 にフォールバック（エラーを返さない）
    return avail[0] if "f1" not in avail else "f1"


# AquesTalk1 のフォールバック声質一覧（ライブラリ未検出時の /version 表示用）。
# 実利用時はロード済み libAquesTalk1-<voice> から動的に組み立てる。
# NOTE: AquesTalk1 は声ごとに別ライブラリで、APIは Synthe_Utf8(koe, speed, size)。
# 声質構造体は使わない（旧実装の AQ1Voice 方式は誤りのため廃止）。
AQ1_VOICE_PRESETS: dict[str, dict[str, int | str]] = {
    "f1": {"id": "f1", "name": "女性1 (F1)"},
    "f2": {"id": "f2", "name": "女性2 (F2)"},
    "f3": {"id": "f3", "name": "女性3 (F3)"},
    "m1": {"id": "m1", "name": "男性1 (M1)"},
    "m2": {"id": "m2", "name": "男性2 (M2)"},
    "r1": {"id": "r1", "name": "ロボット (R1)"},
}

DEFAULT_AQ1_VOICE_ID = "f1"

# AqKanji2Koe が変換しきれなかった入力を検出するための範囲。
# AquesTalk の音声記号列に漢字が残ると code=105 になる。
KANJI_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class MeCabConverter:
    """MeCab+NEologdによる漢字→読み仮名変換（オプション）."""

    def __init__(self, mecab_dic: str = "") -> None:
        self.mecab = None
        self.enabled = False
        if not mecab_dic:
            return
        try:
            import MeCab
            # NEologd辞書を指定してTaggerを作成
            # -d でシステム辞書、-u でユーザー辞書を指定可能
            tagger_args = f"-d {mecab_dic}" if os.path.isdir(mecab_dic) else f"-u {mecab_dic}"
            self.mecab = MeCab.Tagger(tagger_args)
            # ダミーパースで初期化確認
            self.mecab.parse("")
            self.enabled = True
            print(f"[aq_server] MeCab+NEologd enabled: {mecab_dic}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[aq_server] MeCab初期化失敗（スキップ）: {e}", flush=True)
            self.mecab = None
            self.enabled = False

    def to_kana(self, text: str) -> str:
        """漢字混じり文をひらがな/カタカナ読みに変換."""
        if not self.enabled or self.mecab is None:
            return text
        try:
            # MeCabで形態素解析し、読み（yomi）を取得
            node = self.mecab.parseToNode(text)
            result_parts = []
            while node:
                surface = node.surface
                if surface:
                    feature = node.feature.split(",")
                    # feature[7] が読み（yomi）、feature[8] が発音（pronunciation）
                    # NEologd/UniDicの場合、インデックスが異なることがあるため両方試す
                    yomi = None
                    if len(feature) > 8 and feature[8] != "*":
                        yomi = feature[8]  # 発音
                    elif len(feature) > 7 and feature[7] != "*":
                        yomi = feature[7]  # 読み
                    if yomi and yomi != "*":
                        # カタカナ→ひらがな変換
                        yomi = yomi.translate(str.maketrans(
                            "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲンガギグゲゴザジズゼゾダヂヅデドバビブベボパピプペポァィゥェォャュョッー",
                            "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをんがぎぐげござじずぜぞだぢづでどばびぶべぼぱぴぷぺぽぁぃぅぇぉゃゅょっー"
                        ))
                        result_parts.append(yomi)
                    else:
                        result_parts.append(surface)
                node = node.next
            return "".join(result_parts)
        except Exception as e:  # noqa: BLE001
            print(f"[aq_server] MeCab変換エラー（元文を使用）: {e}", flush=True)
            return text


class Engine:
    """実ライブラリのハンドル保持。ロード失敗時は mock/missing として動作."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.mock = args.mock
        self.engine_type: EngineType = (
            getattr(args, "engine", None) or os.environ.get("AQ_ENGINE", "aquestalk10")
        )
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

        self.aq_lib = None  # デフォルトエンジンのlib（後方互換）
        self.aq10_lib = None
        self.aq1_libs: dict[str, object] = {}
        self.aq1_lib_paths: dict[str, str] = {}
        self.aq10_error: str | None = None
        self.aq1_error: str | None = None
        self.kanji_lib = None
        self.kanji_handle = None
        self.load_error: str | None = None

        # --- MeCab+NEologd 初期化（オプション） ---
        self.mecab = MeCabConverter(args.mecab_dic or os.environ.get("AQ_MECAB_DIC", ""))

        if self.mock:
            return

        script_dir = os.path.dirname(os.path.abspath(__file__))
        # --- AquesTalk10 ロード（単一ライブラリ） ---
        aq10_candidates = [p for p in [args.aquestalk_lib] if p] + [
            "./lib/libAquesTalk10.dylib",
            "./lib/libAquesTalk10.so",
            os.path.join(script_dir, "lib", "libAquesTalk10.dylib"),
            os.path.join(script_dir, "lib", "libAquesTalk10.so"),
            "libAquesTalk10.dylib",
            "libAquesTalk10.so",
        ]
        for p in aq10_candidates:
            try:
                if p and os.path.isfile(p):
                    self.aq10_lib = ctypes.CDLL(p)
                    print(f"[aq_server] AquesTalk10 loaded: {p}", flush=True)
                    break
            except OSError:
                continue
        if self.aq10_lib is None:
            self.aq10_error = (
                "AquesTalk10ライブラリが見つかりません。--mock で動作確認するか、"
                "--aquestalk-lib でパスを指定してください。"
            )
        else:
            try:
                if self.dev_key and hasattr(self.aq10_lib, "AquesTalk_SetDevKey"):
                    self.aq10_lib.AquesTalk_SetDevKey(self.dev_key.encode("utf-8"))
                if self.usr_key and hasattr(self.aq10_lib, "AquesTalk_SetUsrKey"):
                    self.aq10_lib.AquesTalk_SetUsrKey(self.usr_key.encode("utf-8"))
            except Exception as e:  # noqa: BLE001 - 起動は継続しエラーを/versionで返す
                self.aq10_error = f"ライセンスキー設定に失敗: {e}"

        # --- AquesTalk1 ロード（声ごとに別ライブラリ: libAquesTalk1-<voice>.dylib） ---
        # 旧来の単一 libAquesTalk.dylib があればそれも使う（voice=f1 扱い）。
        self.aq1_libs, self.aq1_lib_paths = self._load_aq1_libs(args, script_dir)
        if not self.aq1_libs:
            self.aq1_error = (
                "AquesTalk1ライブラリが見つかりません。lib/ に libAquesTalk1-<voice>.dylib "
                "（例: libAquesTalk1-f1.dylib）を配置するか、--aquestalk1-lib でパスを指定してください。"
            )
        else:
            print(
                f"[aq_server] AquesTalk1 loaded voices: {sorted(self.aq1_libs.keys())}",
                flush=True,
            )

        # デフォルトエンジンのlibを aq_lib に反映（後方互換・起動ログ用）
        if self.engine_type == "aquestalk1":
            first = self._default_aq1_lib()
            self.aq_lib = first
            if first is None:
                self.load_error = self.aq1_error
        else:
            self.aq_lib = self.aq10_lib
            if self.aq_lib is None:
                self.load_error = self.aq10_error
        # 両方欠落時のみ全体エラー。片方だけでも起動する。
        if self.aq10_lib is None and not self.aq1_libs:
            self.load_error = self.aq10_error or self.aq1_error or "エンジン未初期化"
        elif self.load_error and (
            (self.engine_type == "aquestalk10" and self.aq10_lib is not None)
            or (self.engine_type == "aquestalk1" and self.aq1_libs)
        ):
            # デフォルトエンジンが使えるなら全体エラーは出さない（他方は engines 欄で個別通知）
            self.load_error = None

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

    # -- AquesTalk1 ローダ --
    def _load_aq1_libs(
        self, args: argparse.Namespace, script_dir: str
    ) -> tuple[dict[str, object], dict[str, str]]:
        """声別 dylib を探索してロードする。戻り値: (voice_id -> CDLL, voice_id -> path)."""
        explicit = (getattr(args, "aquestalk1_lib", "") or "").strip()
        explicit = explicit or os.environ.get("AQ_AQUESTALK1_LIB", "").strip()
        search_dirs: list[str] = []
        explicit_files: list[str] = []
        if explicit:
            if os.path.isdir(explicit):
                search_dirs.append(explicit)
            elif os.path.isfile(explicit):
                explicit_files.append(explicit)
            elif any(c in explicit for c in ("*", "?", "[")):
                explicit_files.extend(sorted(glob.glob(explicit)))
            else:
                # 存在しないパス指定はエラー欄で通知するために候補として保持
                explicit_files.append(explicit)
        search_dirs.extend(
            [
                os.path.join(os.getcwd(), "lib"),
                os.path.join(script_dir, "lib"),
                os.getcwd(),
                script_dir,
            ]
        )
        candidates: list[str] = list(explicit_files)
        seen_dirs: set[str] = set()
        for d in search_dirs:
            ad = os.path.abspath(d)
            if ad in seen_dirs or not os.path.isdir(d):
                continue
            seen_dirs.add(ad)
            for pat in ("libAquesTalk1-*.dylib", "libAquesTalk1-*.so",
                        "libAquesTalk.dylib", "libAquesTalk.so"):
                candidates.extend(sorted(glob.glob(os.path.join(d, pat))))
        libs: dict[str, object] = {}
        paths: dict[str, str] = {}
        for p in candidates:
            if not p or not os.path.isfile(p):
                continue
            base = os.path.basename(p)
            voice_id = ""
            if base.startswith("libAquesTalk1-"):
                # libAquesTalk1-f1.dylib -> f1
                core = base[len("libAquesTalk1-"):]
                voice_id = core.split(".")[0].lower()
            elif base in ("libAquesTalk.dylib", "libAquesTalk.so"):
                voice_id = "f1"  # 旧来の単一ライブラリは f1 扱い
            else:
                continue
            if voice_id in libs:
                continue
            try:
                lib = ctypes.CDLL(p)
            except OSError:
                continue
            try:
                if self.dev_key and hasattr(lib, "AquesTalk_SetDevKey"):
                    lib.AquesTalk_SetDevKey(self.dev_key.encode("utf-8"))
                if self.usr_key and hasattr(lib, "AquesTalk_SetUsrKey"):
                    lib.AquesTalk_SetUsrKey(self.usr_key.encode("utf-8"))
            except Exception:  # noqa: BLE001 - キー設定失敗でも合成は試す
                pass
            libs[voice_id] = lib
            paths[voice_id] = p
        return libs, paths

    def _default_aq1_lib(self) -> Optional[object]:
        if not self.aq1_libs:
            return None
        if DEFAULT_AQ1_VOICE_ID in self.aq1_libs:
            return self.aq1_libs[DEFAULT_AQ1_VOICE_ID]
        return next(iter(self.aq1_libs.values()))

    def aq1_voices(self) -> list[dict[str, str]]:
        """ロード済み Aq1 ライブラリから声一覧を作る（無ければフォールバック定数）."""
        if self.aq1_libs:
            out: list[dict[str, str]] = []
            for vid in sorted(self.aq1_libs.keys()):
                name = AQ1_LIB_VOICE_NAMES.get(vid, f"{vid.upper()} ({vid})")
                out.append({"id": vid, "name": name})
            # f1 を先頭に
            out.sort(key=lambda v: (0 if v["id"] == "f1" else 1, v["id"]))
            # 後方互換: r1 がある場合は robot エイリアスも公開
            if "r1" in self.aq1_libs and "robot" not in self.aq1_libs:
                out.append({"id": "robot", "name": "ロボット (R1・別名)"})
            return out
        return [{"id": v["id"], "name": v["name"]} for v in AQ1_VOICE_PRESETS.values()]

    def aq1_default_voice(self) -> str:
        voices = self.aq1_voices()
        ids = [v["id"] for v in voices]
        if DEFAULT_AQ1_VOICE_ID in ids:
            return DEFAULT_AQ1_VOICE_ID
        return ids[0] if ids else DEFAULT_AQ1_VOICE_ID

    @property
    def kanji_available(self) -> bool:
        return self.mock or (self.kanji_lib is not None and self.kanji_handle is not None)

    @property
    def eval_mode(self) -> bool:
        if self.mock:
            return True
        # デフォルトエンジン基準。Aq1側は評価版の概念がないため常にTrue扱い。
        if self.engine_type == "aquestalk1":
            return True
        return not bool(self.dev_key)

    def version_payload(self) -> dict:
        aq10_voices = [{"id": v["id"], "name": v["name"]} for v in VOICE_PRESETS.values()]
        aq1_voices = self.aq1_voices() if (self.mock or self.aq1_libs) else [
            {"id": v["id"], "name": v["name"]} for v in AQ1_VOICE_PRESETS.values()
        ]
        if self.mock:
            # mock時は両エンジンを利用可として返す（拡張の選択UI確認用）
            return {
                "engine": "aquesTalk10",
                "defaultEngine": "aquestalk10",
                "evalMode": True,
                "kanji": True,
                "voices": aq10_voices,
                "defaultVoice": DEFAULT_VOICE_ID,
                "mock": True,
                "engines": {
                    "aquestalk10": {
                        "available": True,
                        "evalMode": True,
                        "voices": aq10_voices,
                        "defaultVoice": DEFAULT_VOICE_ID,
                    },
                    "aquestalk1": {
                        "available": True,
                        "evalMode": True,
                        "voices": aq1_voices,
                        "defaultVoice": self.aq1_default_voice(),
                    },
                },
            }
        engines = {
            "aquestalk10": {
                "available": self.aq10_lib is not None,
                "evalMode": not bool(self.dev_key),
                "voices": aq10_voices,
                "defaultVoice": DEFAULT_VOICE_ID,
            },
            "aquestalk1": {
                "available": bool(self.aq1_libs),
                "evalMode": True,
                "voices": aq1_voices,
                "defaultVoice": self.aq1_default_voice(),
            },
        }
        if self.aq10_error and self.aq10_lib is None:
            engines["aquestalk10"]["error"] = self.aq10_error
        elif self.aq10_error:
            engines["aquestalk10"]["warning"] = self.aq10_error
        if self.aq1_error and not self.aq1_libs:
            engines["aquestalk1"]["error"] = self.aq1_error

        # 旧クライアント互換: トップレベルはデフォルトエンジンの情報を載せる
        if self.engine_type == "aquestalk1":
            engine_name = "aquesTalk1"
            voices = aq1_voices
            default_voice = self.aq1_default_voice()
            eval_mode = True
            available = bool(self.aq1_libs)
            err = self.aq1_error if not self.aq1_libs else None
        else:
            engine_name = "aquesTalk10"
            voices = aq10_voices
            default_voice = DEFAULT_VOICE_ID
            eval_mode = not bool(self.dev_key)
            available = self.aq10_lib is not None
            err = self.aq10_error if self.aq10_lib is None else None
        if not available:
            return {
                "engine": "not_initialized",
                "defaultEngine": self.engine_type,
                "evalMode": True,
                "kanji": False,
                "voices": voices,
                "defaultVoice": default_voice,
                "engines": engines,
                "error": err or self.load_error or "エンジン未初期化",
            }
        payload = {
            "engine": engine_name,
            "defaultEngine": self.engine_type,
            "evalMode": eval_mode,
            "kanji": self.kanji_available,
            "voices": voices,
            "defaultVoice": default_voice,
            "engines": engines,
        }
        if self.load_error:
            payload["error"] = self.load_error
        return payload

    # -- 合成 --
    def synth(
        self,
        text: str,
        speed: int,
        voice: str | dict | None = None,
        engine: str | None = None,
    ) -> bytes:
        use_engine = normalize_engine(engine, self.engine_type)
        # 実測で Aq1 も 50-300 が有効。両方 50-300 に丸める。
        speed = max(50, min(300, int(speed or 100)))
        if self.mock:
            return mock_wav(text, speed)
        if use_engine == "aquestalk1":
            if not self.aq1_libs:
                raise RuntimeError(
                    self.aq1_error or "AquesTalk1ライブラリが見つかりません"
                )
        else:
            if self.aq10_lib is None:
                raise RuntimeError(
                    self.aq10_error or "AquesTalk10ライブラリが見つかりません"
                )
        koe = self.to_koe(text)
        if KANJI_RE.search(koe):
            raise RuntimeError(
                "漢字を読みへ変換できませんでした。AqKanji2Koeの辞書（aq_dic/aqdic.bin）を確認してください"
            )
        if use_engine == "aquestalk1":
            voice_id = normalize_aq1_voice(
                voice if isinstance(voice, (str, dict)) else (voice or self.aq1_default_voice()),
                list(self.aq1_libs.keys()),
            )
            return self.synth_aq1(koe, speed, voice_id)
        else:
            params = resolve_voice_params(voice)
            return self.synth_aq10(koe, speed, params)

    def synth_aq10(self, koe: str, speed: int, params: dict[str, int]) -> bytes:
        """AquesTalk10合成."""
        lib = self.aq10_lib
        synth_fn = getattr(lib, "AquesTalk_Synthe_Utf8", None) or getattr(
            lib, "AquesTalk_Synth", None
        )
        # 版により FreeWave / FreeWav の表記揺れがある
        free_fn = getattr(lib, "AquesTalk_FreeWave", None) or getattr(
            lib, "AquesTalk_FreeWav", None
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

    def synth_aq1(self, koe: str, speed: int, voice_id: str) -> bytes:
        """AquesTalk1合成。声ごとに別ライブラリを使う。

        APIは Synthe_Utf8(koe, speed, size)（声質構造体なし。声の切替＝libの切替）。
        """
        vid = (voice_id or "").strip().lower()
        vid = AQ1_VOICE_ALIASES.get(vid, vid)
        lib = self.aq1_libs.get(vid)
        if lib is None and vid == "robot" and "r1" in self.aq1_libs:
            lib = self.aq1_libs["r1"]
            vid = "r1"
        if lib is None:
            lib = self._default_aq1_lib()
            vid = self.aq1_default_voice()
        if lib is None:
            raise RuntimeError(self.aq1_error or "AquesTalk1ライブラリが見つかりません")
        synth_fn = getattr(lib, "AquesTalk_Synthe_Utf8", None) or getattr(
            lib, "AquesTalk_Synthe", None
        )
        free_fn = getattr(lib, "AquesTalk_FreeWave", None) or getattr(
            lib, "AquesTalk_FreeWav", None
        )
        if synth_fn is None or free_fn is None:
            raise RuntimeError(
                f"AquesTalk1の音声合成関数が見つかりません（voice={vid}）"
            )
        free_fn.argtypes = [ctypes.c_void_p]
        free_fn.restype = None
        # AquesTalk1: Synthe_Utf8(koe, speed, size)
        synth_fn.argtypes = [
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        synth_fn.restype = ctypes.c_void_p
        size = ctypes.c_int(0)
        ptr = synth_fn(koe.encode("utf-8"), ctypes.c_int(speed), ctypes.byref(size))
        if not ptr or size.value <= 0:
            code = size.value
            reason = AQ1_SYNTH_ERRORS.get(code, "記号列を確認してください")
            raise RuntimeError(f"AquesTalk1合成に失敗しました（code={code}: {reason}）")
        try:
            return ctypes.string_at(ptr, size.value)
        finally:
            try:
                free_fn(ctypes.c_void_p(ptr))
            except Exception:  # noqa: BLE001
                pass

    def resolve_aq1_voice_params(self, voice_arg: str | dict | None) -> dict[str, int]:
        """後方互換のための残骸。Aq1は声質構造体を使わないため空 dict を返す。"""
        return {}

    def to_koe(self, text: str) -> str:
        """漢字混じり文を音声記号列へ変換する。かな文はそのまま返す."""
        # 1. MeCab+NEologdで先に読み変換を試みる（新語・固有名詞・日付表現に強い）
        if self.mecab.enabled:
            text = self.mecab.to_kana(text)
            # MeCabで漢字が残っていなければそのまま返す（AquesTalkはかな入力可）
            if not KANJI_RE.search(text):
                return text
        # 2. AqKanji2Koeで残りの漢字を変換（公式辞書ベース）
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
        req_engine = data.get("engine") or data.get("engineType")
        try:
            wav = self.engine.synth(
                text,
                int(data.get("speed", 100)),
                voice,
                engine=req_engine if isinstance(req_engine, str) else None,
            )
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
    p = argparse.ArgumentParser(description="AquesTalk1/10 最小ローカルサーバー（Chrome拡張用）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=50082)
    p.add_argument("--mock", action="store_true", help="実ライブラリ無しでダミー音声を返す")
    p.add_argument("--engine", choices=["aquestalk10", "aquestalk1"], default=None,
                   help="デフォルトエンジン: aquestalk10 (既定) または aquestalk1。両方ロードし、/synth の engine 指定で切替可")
    p.add_argument("--aquestalk-lib", default="", help="AquesTalk10ライブラリのパス（指定しない場合は自動探索）")
    p.add_argument("--aquestalk1-lib", default="",
                   help="AquesTalk1ライブラリのパスまたはディレクトリ・glob（指定しない場合は lib/ を自動探索。例: ./lib/libAquesTalk1-f1.dylib, ./lib）")
    p.add_argument("--kanji-lib", default="")
    p.add_argument("--dic-dir", default="./aq_dic")
    p.add_argument("--mecab-dic", default="", help="MeCab+NEologd辞書パス（-d または -u で指定するディレクトリ/ファイル）")
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
    # 環境変数を読み込んでからEngineを初期化
    Handler.engine = Engine(args)
    payload = Handler.engine.version_payload()
    if args.mock:
        mode = "mock"
    elif Handler.engine.aq10_lib is not None and Handler.engine.aq1_libs:
        mode = "real(both)"
    elif Handler.engine.aq10_lib is not None or Handler.engine.aq1_libs:
        mode = "real"
    else:
        mode = "missing-lib"
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
