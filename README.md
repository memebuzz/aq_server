# aq_server — AquesTalk10 最小ローカルサーバー（Chrome拡張用）

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
# lib/ に dylib/so、aq_dic/ に辞書を配置
python3 aq_server.py --port 50082 \
  --aquestalk-lib ./lib/libAquesTalk10.dylib \
  --kanji-lib ./lib/libAqKanji2Koe.dylib \
  --dic-dir ./aq_dic
```

## エンドポイント

| Method | Path | 説明 |
|--------|------|------|
| GET | `/version` | `{engine, evalMode, kanji, voices, defaultVoice}` を返す |
| POST | `/synth` | `{text, speed: 50-300, voice: "reimu"|"marisa"|...}` → WAV |
| GET | `/health` | 疎通確認用 |

### 利用可能なボイスプリセット

| ID | 名称 | 特徴 |
|----|------|------|
| `reimu` (デフォルト) | ゆっくり霊夢（女声1） | F1Eベース。王道のゆっくり実況ボイス |
| `marisa` | ゆっくり魔理沙（女声2） | F2Eベース。ハキハキとした相方ボイス |
| `yukkuri_f3` | 女声3（高音・F3） | F1Eベース・高音寄り |
| `yukkuri_m1` | 男声1（M1） | M1Eベース・男性ボイス |
| `yukkuri_r1` | ロボット1（R1） | M1Eベース・ロボット調 |


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
