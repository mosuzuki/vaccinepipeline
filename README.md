# Vaccine Development Pipeline Dashboard

感染症ワクチン開発パイプラインを自動更新するための、GitHub Pages向け静的ダッシュボードです。既存の Vaccine & Immunization News Dashboard とは別リポジトリとして運用する想定です。

## できること

- ClinicalTrials.gov API v2 から感染症ワクチン関連の臨床試験を取得
- 疾患、フェーズ、技術プラットフォーム、ステータス、スポンサー、国で可視化
- 検索・フィルタ・CSVダウンロード
- GitHub Actions による週次自動更新
- `config.yml` に対象疾患を追加するだけで範囲拡張可能

## ファイル構成

```text
.
├── index.html
├── assets/
│   ├── app.js
│   └── styles.css
├── data/
│   ├── pipeline.json
│   └── summary.json
├── scripts/
│   └── fetch_pipeline.py
├── config.yml
├── requirements.txt
└── .github/workflows/update-pipeline.yml
```

## セットアップ

1. GitHubで新規リポジトリを作成します。
2. このフォルダの中身をそのままアップロードします。
3. GitHub Pages を有効化します。
   - Settings → Pages
   - Source: Deploy from a branch
   - Branch: `main` / root
4. Actions タブから `Update vaccine pipeline dashboard` を手動実行します。
5. 実行後、`data/pipeline.json` と `data/summary.json` が更新されます。

## ローカル確認

```bash
pip install -r requirements.txt
python scripts/fetch_pipeline.py
python -m http.server 8000
```

ブラウザで `http://localhost:8000` を開きます。

## データ源

### 自動取得

- ClinicalTrials.gov API v2

### 手動レビュー・今後の拡張候補

- WHO Vaccine Pipeline Tracker
- WHO COVID-19 vaccine tracker and landscape
- CIDRAP Coronavirus Vaccine Technology Landscape
- PATH RSV and mAb Trial Tracker
- TB Vaccine Pipeline
- IAVI Pipeline
- Vaccines Europe Pipeline Dashboard
- FDA / EMA / WHO PQ / WHO EUL
- 企業パイプラインページ

## 重要な制約

ClinicalTrials.gov は「試験単位」のデータです。したがって、同一候補ワクチンが複数試験として表示されることがあります。候補ワクチン単位で正確に数えるには、候補名・スポンサー・抗原・プラットフォームによる名寄せロジック、または手動curationが必要です。

また、前臨床候補は臨床試験レジストリには十分に反映されません。WHO、CEPI、PATH、IAVI、TB Vaccine Pipeline、企業発表などを追加ソースとして統合する必要があります。

## 次に追加するとよい機能

- 候補ワクチン単位の deduplication table
- 新規追加・フェーズ進行・中止・完了をイベントとして検出する差分ログ
- WHO PQ / EUL / FDA / EMA 承認状況の列
- preclinical candidates の curated CSV 取り込み
- 日本語要約欄とAIサマリー
- 疾患別の重点リスト：Disease X、VHF、respiratory、AMR関連細菌ワクチンなど
