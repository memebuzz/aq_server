# aq_server — AquesTalk10 最小ローカルサーバー（Chrome拡張用）

`memebuzz_chrome_extension`（`src/lib/aqserver.ts`）は `http://127.0.0.1:50082` のこのサーバーにだけ接続します。
標準ライブラリのみ・`pip install` 不要です。

## 起動

```bash
cd projects/aq_server

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
| GET | `/version` | `{engine, evalMode, kanji}` を返す |
| POST | `/synth` | `{text, speed: 50-300}` → WAV |
| GET | `/health` | 疎通確認用 |

## ライセンス注意

- 開発キー未設定でも起動しますが評価版動作（ナ行・マ行→ヌ）です。
- `AQ_DEV_KEY` / `AQ_USR_KEY` / `AQ_KANJI_DEV_KEY` 環境変数か引数で指定できます。
- 製品利用には開発・使用／頒布ライセンスが必要です。
