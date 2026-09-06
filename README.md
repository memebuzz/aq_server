# aq_server — AquesTalk1/10 最小ローカルサーバー（Chrome拡張用）

`memebuzz_chrome_extension`（`src/lib/aqserver.ts`）は `http://127.0.0.1:50082` のこのサーバーにだけ接続します。
標準ライブラリのみ・`pip install` 不要です。

## 起動

```bash
git clone https://github.com/memebuzz/aq_server
cd aq_server

# 1) 配線確認（ライブラリ不要・ダミー音声）
python3 aq_server.py --mock --port 50082
curl http://127.0.0.1:50082/version
# {"engine": "aquesTalk10", ...} が返ればOK

# 2) 実ライブラリ使用（アクエストから別途取得）
# lib/ に dylib/so、aq_dic/ に辞書を配置。
# AquesTalk10 と AquesTalk1 は同居できます（見つかった分だけ両方ロード）。
# Chrome拡張側の「AquesTalk10 / AquesTalk1」切替はリクエストごとの engine 指定で行います。
python3 aq_server.py --port 50082 \
  --aquestalk-lib ./lib/libAquesTalk10.dylib \
  --aquestalk1-lib ./lib \
  --kanji-lib ./lib/libAqKanji2Koe.dylib \
  --dic-dir ./aq_dic

# ※ AquesTalk1 は声ごとに別ライブラリです（単一の libAquesTalk.dylib ではありません）。
#    lib/ に libAquesTalk1-f1.dylib, libAquesTalk1-m1.dylib, … を配置してください。
#    旧来の単一 libAquesTalk.dylib がある場合は f1 扱いで使います。
# ※ --engine / AQ_ENGINE はデフォルトエンジン（旧クライアント互換表示用）の指定です。
#    省略時は aquestalk10。両方ある場合も /synth の engine 指定でいつでも切替できます。

# 3) MeCab+NEologdによる漢字読み精度向上（pip install mecab-python3 等が必要）:
  # リポジトリにサブモジュールとして含めています（projects/mecab-ipadic-neologd）
  # 初回のみビルドが必要:
  cd ../mecab-ipadic-neologd
  ./bin/install-mecab-ipadic-neologd -n -y  # システム辞書としてインストール（要sudo）
  # またはユーザー辞書としてビルド（sudo不要）:
  ./bin/install-mecab-ipadic-neologd -n --prefix=$(pwd)/build
  cd ../aq_server

  # 起動（ビルド済み辞書を指定）:
  python3 aq_server.py --port 50082 \
    --engine aquestalk10 \
    --aquestalk-lib ./lib/libAquesTalk10.dylib \
    --kanji-lib ./lib/libAqKanji2Koe.dylib \
    --dic-dir ./aq_dic \
    --mecab-dic ../mecab-ipadic-neologd/build/lib/mecab/dic/ipadic-neologd

# または .env に記述（リポジトリルートから起動する場合）:
  AQ_MECAB_DIC=../mecab-ipadic-neologd/build/lib/mecab/dic/ipadic-neologd
```

## エンドポイント

| Method | Path | 説明 |
|--------|------|------|
| GET | `/version` | `{engine, defaultEngine, evalMode, kanji, voices, defaultVoice, engines: {aquestalk10, aquestalk1}}` を返す |
| POST | `/synth` | `{text, speed: 50-300, voice: "reimu"\|"f1"\|..., engine: "aquestalk10"\|"aquestalk1"}` → WAV（`engine` 省略時はデフォルトエンジン） |
| GET | `/health` | 疎通確認用 |

### 利用可能なボイスプリセット

#### AquesTalk10

| ID | 名称 | 特徴 |
|----|------|------|
| `reimu` (デフォルト) | ゆっくり霊夢（女声1） | F1Eベース。王道のゆっくり実況ボイス |
| `marisa` | ゆっくり魔理沙（女声2） | F2Eベース。ハキハキとした相方ボイス |
| `yukkuri_f3` | 女声3（高音・F3） | F1Eベース・高音寄り |
| `yukkuri_m1` | 男声1（M1） | M1Eベース・男性ボイス |
| `yukkuri_r1` | ロボット1（R1） | M1Eベース・ロボット調 |

#### AquesTalk1（配置した声別ライブラリから動的に公開）

| ID | 名称 | 対応ライブラリ |
|----|------|------|
| `f1` (デフォルト) | 女性1 (F1) | libAquesTalk1-f1.dylib |
| `f2` | 女性2 (F2) | libAquesTalk1-f2.dylib |
| `f3` | 女性3 (F3) | libAquesTalk1-f3.dylib |
| `m1` | 男性1 (M1) | libAquesTalk1-m1.dylib |
| `m2` | 男性2 (M2) | libAquesTalk1-m2.dylib |
| `r1`（別名 `robot`） | ロボット (R1) | libAquesTalk1-r1.dylib |
| `dvd` / `jgr` / `imd1` | （同名の声） | 対応する libAquesTalk1-*.dylib |

※ 話速は両エンジンとも 50-300 を受け付けます。


## ライセンス注意

- 開発キー未設定でも起動しますが評価版動作（ナ行・マ行→ヌ）です。
- 優先順位: コマンド引数 > 環境変数 > `.env` ファイル
- `.env` を使う場合（`pip install` 不要・標準ライブラリのみで読み込みます）:

```bash
cp .env.example .env   # AQ_DEV_KEY / AQ_USR_KEY / AQ_KANJI_DEV_KEY を記入
python3 aq_server.py --port 50082
# 別の場所の .env を使う場合
python3 aq_server.py --port 50082 --env-file /path/to/.env
```

- `.env` はカレントディレクトリか `aq_server.py` と同じ場所に置くと自動で読み込まれます
  （読み込むと起動ログに `[aq_server] .env 読み込み: ...` と出ます）。
- 製品利用には開発・使用／頒布ライセンスが必要です。

## 辞書について

- `aq_dic/` にはアクエスト配布の辞書ファイル（`aqdic.bin`・`aq_user.dic`）を配置してください。
- 辞書が無い／読めない場合はクラッシュせず、かな直接入力モード（`kanji: false`）で起動します。
  `/version` の `error` に理由が表示されます。
