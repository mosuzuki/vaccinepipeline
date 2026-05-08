# 感染症ワクチン開発パイプライン・ダッシュボード（試行版）

公開情報に基づく感染症ワクチン開発パイプラインを、GitHub Pagesで軽量に可視化する試行版ダッシュボードです。

The dashboard is intentionally separate from the Vaccine & Immunization news dashboard. It focuses on **candidate vaccines** and classifies each candidate into:

- Discovery
- Preclinical
- Early Phase 1
- Phase 1
- Phase 1/2
- Phase 2
- Phase 2/3
- Phase 3
- Phase 4
- Approved/Authorized

## What it does

`scripts/fetch_pipeline.py` builds three data files:

- `data/pipeline.json` — candidate-level records used by the dashboard
- `data/studies.json` — supporting ClinicalTrials.gov trial-level records
- `data/summary.json` — summary counts, recent updates, and source metadata

The script combines:

1. **ClinicalTrials.gov API v2**: automated search of vaccine-related clinical trials, then approximate grouping from trial records into candidate-level records.
2. **Best-effort curated public sources**: TB preclinical/clinical pipeline pages, PATH RSV tracker, PATH GBS tracker, WHO mpox tracker, CIDRAP coronavirus landscape, CIDRAP universal influenza landscape, and IAVI pipeline, when parsable as public tables/spreadsheets/HTML.
3. **Public-source seed data**: the repository ships with real public-source seed records so the dashboard is not blank or artificial before the first successful GitHub Actions run.
4. **Manual reference sources**: WHO vaccine pipeline tracker, WHO malaria clinical development review, Vaccines Europe, and Impact Global Health are listed as reference sources and can be automated later if a stable data format is available.

## Why preclinical needs curated sources

ClinicalTrials.gov mainly contains human clinical trial records, so it cannot fully capture preclinical vaccine candidates. Preclinical candidates require curated pipeline sources such as disease-specific tracker tables, WHO landscape documents, CIDRAP, TB Vaccine Pipeline, PATH, or developer pipelines.

## Local run

```bash
python -m pip install -r requirements.txt
python scripts/fetch_pipeline.py
python -m http.server 8000
```

Then open:

```text
http://localhost:8000
```

## GitHub Pages deployment

1. Upload these files to a new GitHub repository.
2. Go to **Settings → Pages**.
3. Set **Source** to **Deploy from a branch**.
4. Select `main` and `/root`.
5. Save.
6. Go to **Actions → Update vaccine pipeline dashboard → Run workflow**.

After the first run, the dashboard will update weekly by GitHub Actions. If live source fetching fails because of a temporary network/DNS problem, the script keeps the real public-source seed dataset rather than reverting to artificial sample rows.

## Editing target diseases

Edit `config.yml`, especially:

```yaml
clinicaltrials:
  diseases:
    - id: rsv
      label: RSV
      condition: Respiratory Syncytial Virus Infections
```

To add a disease, add a new block with `id`, `label`, `condition`, and `category`.

## Adding preclinical / curated sources

Add a table-like source under `automated_curated_sources`:

```yaml
automated_curated_sources:
  - name: Example Preclinical Pipeline
    id_prefix: EXAMPLE
    url: https://example.org/pipeline-table
    disease: Example disease
    disease_id: example
    default_stage: Preclinical
    enabled: true
```

The script tries to detect common columns such as candidate, vaccine candidate, product, stage, phase, platform, technology, developer, and sponsor.

## Important limitations

- ClinicalTrials.gov is trial-level, not product-level. Candidate grouping is approximate.
- Preclinical coverage is incomplete and depends on publicly parsable curated sources.
- Some public dashboards are interactive or manually maintained and may not expose stable machine-readable data.
- Commercial sources such as Citeline, AdisInsight, and GlobalData are not included because of licensing restrictions.
