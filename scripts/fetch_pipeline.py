#!/usr/bin/env python3
"""Fetch vaccine clinical-trial pipeline data from ClinicalTrials.gov API v2.

This script intentionally starts with public, free, no-key sources. It produces:
- data/pipeline.json: normalized study-level dataset for the dashboard
- data/summary.json: counts and last-refresh metadata

Notes:
- ClinicalTrials.gov records are trial-level, not product-level. Candidate/product
  deduplication is approximated from intervention names and should be curated later.
- WHO ICTRP web services generally require partner access. The dashboard keeps it
  as a documented future source rather than scraping the portal.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlencode

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yml"
DATA_DIR = ROOT / "data"

PHASE_ORDER = {
    "EARLY_PHASE1": "Early Phase 1",
    "PHASE1": "Phase 1",
    "PHASE1/PHASE2": "Phase 1/2",
    "PHASE2": "Phase 2",
    "PHASE2/PHASE3": "Phase 2/3",
    "PHASE3": "Phase 3",
    "PHASE4": "Phase 4",
    "NA": "Not Applicable",
}

PLATFORM_RULES = [
    (r"mRNA|messenger RNA|RNA[- ]based", "mRNA"),
    (r"DNA vaccine|plasmid DNA", "DNA"),
    (r"viral vector|adenovirus|adeno|MVA|modified vaccinia|VSV|vesicular stomatitis", "Viral vector"),
    (r"protein|subunit|recombinant|VLP|virus[- ]like particle", "Protein/subunit/VLP"),
    (r"inactivated|killed|whole[- ]virion", "Inactivated"),
    (r"live attenuated|attenuated", "Live attenuated"),
    (r"conjugate|polysaccharide", "Conjugate/polysaccharide"),
    (r"outer membrane vesicle|OMV", "OMV"),
    (r"peptide", "Peptide"),
    (r"monoclonal antibody|mAb|nirsevimab|palivizumab", "Preventive mAb"),
]


def load_config() -> Dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(clean_text(v) for v in value if v)
    return re.sub(r"\s+", " ", str(value)).strip()


def first(value: Any, default: str = "") -> str:
    if isinstance(value, list):
        return clean_text(value[0]) if value else default
    return clean_text(value) or default


def classify_platform(text: str) -> str:
    haystack = text or ""
    for pattern, label in PLATFORM_RULES:
        if re.search(pattern, haystack, flags=re.I):
            return label
    return "Unclassified"


def phase_label(phases: Iterable[str] | None) -> str:
    if not phases:
        return "Unknown"
    labels = [PHASE_ORDER.get(p, p.replace("_", " ").title()) for p in phases]
    return ", ".join(labels) if labels else "Unknown"


def extract_locations(protocol: Dict[str, Any]) -> List[str]:
    contacts = protocol.get("contactsLocationsModule", {})
    locations = contacts.get("locations", []) or []
    countries = sorted({clean_text(loc.get("country")) for loc in locations if loc.get("country")})
    return countries


def extract_study(study: Dict[str, Any], disease: Dict[str, str]) -> Dict[str, Any]:
    protocol = study.get("protocolSection", {})
    identification = protocol.get("identificationModule", {})
    status = protocol.get("statusModule", {})
    design = protocol.get("designModule", {})
    conditions = protocol.get("conditionsModule", {})
    arms = protocol.get("armsInterventionsModule", {})
    sponsor = protocol.get("sponsorCollaboratorsModule", {})

    nct_id = identification.get("nctId", "")
    interventions = arms.get("interventions", []) or []
    intervention_names = [clean_text(i.get("name")) for i in interventions if i.get("name")]
    intervention_desc = " ; ".join(
        [clean_text(i.get("description")) for i in interventions if i.get("description")]
    )
    intervention_text = " ; ".join(intervention_names + [intervention_desc])

    lead_sponsor = sponsor.get("leadSponsor", {}).get("name", "")
    collaborators = [c.get("name", "") for c in sponsor.get("collaborators", []) or []]

    return {
        "id": nct_id,
        "source": "ClinicalTrials.gov",
        "url": f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else "",
        "title": clean_text(identification.get("briefTitle")),
        "official_title": clean_text(identification.get("officialTitle")),
        "disease_id": disease["id"],
        "disease": disease["label"],
        "disease_category": disease.get("category", ""),
        "conditions": conditions.get("conditions", []) or [],
        "candidate": first(intervention_names, "Unspecified vaccine candidate"),
        "interventions": intervention_names,
        "platform": classify_platform(intervention_text),
        "phase": phase_label(design.get("phases")),
        "study_type": clean_text(design.get("studyType")),
        "overall_status": clean_text(status.get("overallStatus")),
        "start_date": clean_text((status.get("startDateStruct") or {}).get("date")),
        "completion_date": clean_text((status.get("completionDateStruct") or {}).get("date")),
        "last_update": clean_text((status.get("lastUpdatePostDateStruct") or {}).get("date")),
        "enrollment": (design.get("enrollmentInfo") or {}).get("count"),
        "lead_sponsor": clean_text(lead_sponsor),
        "collaborators": [clean_text(c) for c in collaborators if c],
        "countries": extract_locations(protocol),
    }


def is_likely_vaccine(record: Dict[str, Any]) -> bool:
    text = " ".join(
        [
            record.get("title", ""),
            record.get("official_title", ""),
            record.get("candidate", ""),
            " ".join(record.get("interventions", [])),
        ]
    )
    return bool(re.search(r"vaccine|vaccination|immuni[sz]ation|mAb|monoclonal antibody", text, flags=re.I))


def fetch_disease(base_url: str, disease: Dict[str, str], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    query = cfg["query_template"].format(condition=disease["condition"])
    page_token: Optional[str] = None
    page_size = int(cfg.get("page_size", 100))
    max_pages = int(cfg.get("max_pages_per_disease", 5))
    records: List[Dict[str, Any]] = []

    for _ in range(max_pages):
        params = {
            "query.term": query,
            "pageSize": page_size,
            "format": "json",
        }
        if page_token:
            params["pageToken"] = page_token
        url = f"{base_url}?{urlencode(params)}"
        response = requests.get(url, timeout=40)
        response.raise_for_status()
        payload = response.json()
        for study in payload.get("studies", []) or []:
            rec = extract_study(study, disease)
            if is_likely_vaccine(rec):
                records.append(rec)
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    return records


def deduplicate(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        key = rec.get("id") or f"{rec.get('disease')}::{rec.get('title')}"
        if key not in by_id:
            by_id[key] = rec
        else:
            # Preserve multiple disease tags if a trial matches several searches.
            existing = by_id[key]
            diseases = sorted(set([existing.get("disease", ""), rec.get("disease", "")]))
            existing["disease"] = " / ".join([d for d in diseases if d])
            disease_ids = sorted(set([existing.get("disease_id", ""), rec.get("disease_id", "")]))
            existing["disease_id"] = ",".join([d for d in disease_ids if d])
    return sorted(by_id.values(), key=lambda r: (r.get("disease", ""), r.get("phase", ""), r.get("candidate", "")))


def make_summary(records: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    by_phase = Counter(r.get("phase", "Unknown") for r in records)
    by_disease = Counter(r.get("disease", "Unknown") for r in records)
    by_platform = Counter(r.get("platform", "Unclassified") for r in records)
    active_statuses = {"RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION", "NOT_YET_RECRUITING"}
    active = [r for r in records if r.get("overall_status") in active_statuses]
    recent_updates = sorted(
        [r for r in records if r.get("last_update")],
        key=lambda r: r.get("last_update", ""),
        reverse=True,
    )[:20]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "active_record_count": len(active),
        "disease_count": len(by_disease),
        "phase_counts": dict(by_phase),
        "disease_counts": dict(by_disease),
        "platform_counts": dict(by_platform),
        "recent_updates": recent_updates,
        "sources": [
            {"name": "ClinicalTrials.gov API v2", "url": cfg["clinicaltrials"]["base_url"], "status": "automated"}
        ] + cfg.get("curated_sources", []),
        "notes": [
            "ClinicalTrials.gov records are trial-level; candidate-level deduplication is approximate.",
            "WHO ICTRP and disease-specific trackers should be used as curated validation sources.",
            "Preclinical candidates are underrepresented unless added from curated sources."
        ],
    }


def write_outputs(records: List[Dict[str, Any]], summary: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "pipeline.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def fallback_sample() -> List[Dict[str, Any]]:
    # Small synthetic sample so the dashboard renders before the first scheduled fetch.
    today = datetime.now(timezone.utc).date().isoformat()
    return [
        {
            "id": "SAMPLE-RSV-001",
            "source": "Sample data",
            "url": "",
            "title": "Example RSV vaccine candidate trial",
            "official_title": "",
            "disease_id": "rsv",
            "disease": "RSV",
            "disease_category": "respiratory",
            "conditions": ["Respiratory Syncytial Virus Infections"],
            "candidate": "Example RSV prefusion F vaccine",
            "interventions": ["RSV vaccine"],
            "platform": "Protein/subunit/VLP",
            "phase": "Phase 3",
            "study_type": "INTERVENTIONAL",
            "overall_status": "RECRUITING",
            "start_date": "2025-01",
            "completion_date": "2027-06",
            "last_update": today,
            "enrollment": 12000,
            "lead_sponsor": "Example sponsor",
            "collaborators": [],
            "countries": ["Japan", "United States"],
        },
        {
            "id": "SAMPLE-TB-001",
            "source": "Sample data",
            "url": "",
            "title": "Example tuberculosis vaccine study",
            "official_title": "",
            "disease_id": "tuberculosis",
            "disease": "Tuberculosis",
            "disease_category": "bacterial",
            "conditions": ["Tuberculosis"],
            "candidate": "Example TB vaccine",
            "interventions": ["Tuberculosis vaccine"],
            "platform": "Viral vector",
            "phase": "Phase 2",
            "study_type": "INTERVENTIONAL",
            "overall_status": "ACTIVE_NOT_RECRUITING",
            "start_date": "2024-04",
            "completion_date": "2026-12",
            "last_update": today,
            "enrollment": 2500,
            "lead_sponsor": "Example PDP",
            "collaborators": [],
            "countries": ["South Africa", "Kenya"],
        },
    ]


def main() -> int:
    cfg = load_config()
    ct_cfg = cfg["clinicaltrials"]
    all_records: List[Dict[str, Any]] = []
    try:
        for disease in ct_cfg["diseases"]:
            print(f"Fetching {disease['label']}...", file=sys.stderr)
            all_records.extend(fetch_disease(ct_cfg["base_url"], disease, ct_cfg))
        records = deduplicate(all_records)
        if not records:
            raise RuntimeError("No records returned from ClinicalTrials.gov")
    except Exception as exc:
        print(f"WARNING: fetch failed, writing sample dataset instead: {exc}", file=sys.stderr)
        records = fallback_sample()
    summary = make_summary(records, cfg)
    write_outputs(records, summary)
    print(f"Wrote {len(records)} records to data/pipeline.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
