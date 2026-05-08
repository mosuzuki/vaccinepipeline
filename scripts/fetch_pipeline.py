#!/usr/bin/env python3
"""Build a candidate-level infectious-disease vaccine pipeline dataset.

Outputs
- data/pipeline.json: one row per candidate/source/disease, with stages Preclinical and Phase 1-4
- data/studies.json: supporting trial-level records from ClinicalTrials.gov
- data/summary.json: dashboard metadata and counts

Data strategy
1) ClinicalTrials.gov API v2 is queried automatically and grouped from trial-level
   records into approximate candidate-level records.
2) Disease-specific public trackers that expose HTML/XLSX tables are harvested on a
   best-effort basis. These are especially important for preclinical candidates.
3) The script is intentionally conservative: if a curated source layout changes,
   it logs the issue and continues, rather than breaking the dashboard update.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import pandas as pd
import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yml"
DATA_DIR = ROOT / "data"

ACTIVE_STATUSES = {"RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION", "NOT_YET_RECRUITING"}
TERMINAL_STATUSES = {"TERMINATED", "WITHDRAWN", "SUSPENDED"}

STAGE_ORDER = {
    "Discovery": 0,
    "Preclinical": 1,
    "Early Phase 1": 2,
    "Phase 1": 3,
    "Phase 1/2": 4,
    "Phase 2": 5,
    "Phase 2/3": 6,
    "Phase 3": 7,
    "Phase 4": 8,
    "Approved/Authorized": 9,
    "Unknown": -1,
    "Not Applicable": -1,
}

PHASE_MAP = {
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
    (r"\bmRNA\b|messenger RNA|RNA[- ]based|self[- ]amplifying RNA|saRNA", "mRNA/RNA"),
    (r"DNA vaccine|plasmid DNA|\bDNA\b", "DNA"),
    (r"viral vector|adenovirus|adeno|ChAd|MVA|modified vaccinia|VSV|vesicular stomatitis|measles vector|poxvirus vector", "Viral vector"),
    (r"protein|subunit|recombinant|RBD|spike|VLP|virus[- ]like particle|nanoparticle|particle", "Protein/subunit/VLP"),
    (r"inactivated|killed|whole[- ]virion|whole virus", "Inactivated"),
    (r"live attenuated|attenuated|cold[- ]adapted", "Live attenuated"),
    (r"conjugate|polysaccharide|PCV|PPSV", "Conjugate/polysaccharide"),
    (r"outer membrane vesicle|\bOMV\b", "OMV"),
    (r"peptide", "Peptide"),
    (r"monoclonal antibody|\bmAb\b|nirsevimab|palivizumab|clesrovimab", "Preventive mAb"),
]

CANDIDATE_STOPWORDS = re.compile(
    r"\b(placebo|saline|control|standard of care|booster dose|dose [0-9]+|experimental|comparator)\b",
    flags=re.I,
)


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", file=sys.stderr)


def load_config() -> Dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def clean_text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, (list, tuple, set)):
        return "; ".join(clean_text(v) for v in value if clean_text(v))
    return re.sub(r"\s+", " ", str(value)).strip()


def norm_key(text: str) -> str:
    text = clean_text(text).lower()
    text = re.sub(r"[^a-z0-9α-ω一-龥ぁ-んァ-ヶ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def make_id(prefix: str, parts: Iterable[str]) -> str:
    raw = "||".join(clean_text(p) for p in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def classify_platform(text: str) -> str:
    for pattern, label in PLATFORM_RULES:
        if re.search(pattern, text or "", flags=re.I):
            return label
    return "Unclassified"


def normalize_stage(text: Any) -> str:
    s = clean_text(text)
    if not s:
        return "Unknown"
    t = s.lower().replace("phase", "phase ")
    t = re.sub(r"\s+", " ", t)
    if re.search(r"approved|authori[sz]ed|licensed|licensure|prequalified|pq\b", t):
        return "Approved/Authorized"
    if re.search(r"phase\s*4|post[- ]?marketing|post[- ]?licen", t):
        return "Phase 4"
    if re.search(r"phase\s*2\s*/\s*3|phase\s*ii\s*/\s*iii|phase\s*2b\s*/\s*3", t):
        return "Phase 2/3"
    if re.search(r"phase\s*1\s*/\s*2|phase\s*i\s*/\s*ii|phase\s*1b\s*/\s*2", t):
        return "Phase 1/2"
    if re.search(r"phase\s*3|phase\s*iii|pivotal", t):
        return "Phase 3"
    if re.search(r"phase\s*2|phase\s*ii", t):
        return "Phase 2"
    if re.search(r"early phase\s*1|phase\s*0", t):
        return "Early Phase 1"
    if re.search(r"phase\s*1|phase\s*i\b|first[- ]in[- ]human", t):
        return "Phase 1"
    if re.search(r"pre[- ]?clinical|nonclinical|animal|proof[- ]of[- ]concept", t):
        return "Preclinical"
    if re.search(r"discovery|exploratory", t):
        return "Discovery"
    return s if s in STAGE_ORDER else "Unknown"


def stage_from_ctg_phases(phases: Optional[List[str]]) -> str:
    if not phases:
        return "Unknown"
    stages = [PHASE_MAP.get(p, normalize_stage(p)) for p in phases]
    return max(stages, key=lambda x: STAGE_ORDER.get(x, -1)) if stages else "Unknown"


def max_stage(stages: Iterable[str]) -> str:
    stages = [normalize_stage(s) for s in stages if clean_text(s)]
    return max(stages, key=lambda x: STAGE_ORDER.get(x, -1)) if stages else "Unknown"


def extract_locations(protocol: Dict[str, Any]) -> List[str]:
    contacts = protocol.get("contactsLocationsModule", {})
    locs = contacts.get("locations", []) or []
    return sorted({clean_text(loc.get("country")) for loc in locs if clean_text(loc.get("country"))})


def candidate_from_interventions(interventions: List[Dict[str, Any]], title: str) -> str:
    names = [clean_text(i.get("name")) for i in interventions if clean_text(i.get("name"))]
    vaccine_names = []
    for name in names:
        if CANDIDATE_STOPWORDS.search(name):
            continue
        if re.search(r"vaccine|vaccination|immuni[sz]|mAb|monoclonal|BNT|mRNA|ChAd|MVA|BCG|RSV|PCV|PPSV", name, re.I):
            vaccine_names.append(name)
    if vaccine_names:
        vaccine_names.sort(key=len)
        return vaccine_names[0]
    if names:
        return names[0]
    m = re.search(r"(?:of|with|using)\s+([A-Z0-9][A-Za-z0-9 ._+\-/]{2,80}?(?:vaccine|mAb|antibody))", title, re.I)
    return clean_text(m.group(1)) if m else "Unspecified vaccine candidate"


def extract_study(study: Dict[str, Any], disease: Dict[str, str]) -> Dict[str, Any]:
    protocol = study.get("protocolSection", {})
    identification = protocol.get("identificationModule", {})
    status = protocol.get("statusModule", {})
    design = protocol.get("designModule", {})
    conditions = protocol.get("conditionsModule", {})
    arms = protocol.get("armsInterventionsModule", {})
    sponsor = protocol.get("sponsorCollaboratorsModule", {})
    interventions = arms.get("interventions", []) or []
    names = [clean_text(i.get("name")) for i in interventions if clean_text(i.get("name"))]
    descriptions = [clean_text(i.get("description")) for i in interventions if clean_text(i.get("description"))]
    title = clean_text(identification.get("briefTitle"))
    official_title = clean_text(identification.get("officialTitle"))
    candidate = candidate_from_interventions(interventions, f"{title} {official_title}")
    lead = clean_text((sponsor.get("leadSponsor") or {}).get("name"))
    collaborators = [clean_text(c.get("name")) for c in sponsor.get("collaborators", []) or [] if clean_text(c.get("name"))]
    nct_id = clean_text(identification.get("nctId"))
    text_for_class = " ; ".join([candidate, title, official_title] + names + descriptions)
    return {
        "study_id": nct_id,
        "source": "ClinicalTrials.gov",
        "source_url": f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else "",
        "title": title,
        "official_title": official_title,
        "disease_id": disease["id"],
        "disease": disease["label"],
        "disease_category": disease.get("category", ""),
        "conditions": conditions.get("conditions", []) or [],
        "candidate": candidate,
        "candidate_key": norm_key(candidate),
        "interventions": names,
        "platform": classify_platform(text_for_class),
        "stage": stage_from_ctg_phases(design.get("phases")),
        "study_phase_raw": clean_text(design.get("phases")),
        "study_type": clean_text(design.get("studyType")),
        "overall_status": clean_text(status.get("overallStatus")),
        "start_date": clean_text((status.get("startDateStruct") or {}).get("date")),
        "completion_date": clean_text((status.get("completionDateStruct") or {}).get("date")),
        "last_update": clean_text((status.get("lastUpdatePostDateStruct") or {}).get("date")),
        "enrollment": (design.get("enrollmentInfo") or {}).get("count"),
        "lead_sponsor": lead,
        "collaborators": collaborators,
        "countries": extract_locations(protocol),
    }


def is_likely_vaccine_study(rec: Dict[str, Any]) -> bool:
    text = " ".join([
        rec.get("title", ""), rec.get("official_title", ""), rec.get("candidate", ""),
        " ".join(rec.get("interventions", [])), " ".join(rec.get("conditions", [])),
    ])
    if re.search(r"therapeutic vaccine|cancer vaccine|tumou?r vaccine", text, re.I):
        return False
    return bool(re.search(r"vaccine|vaccination|immuni[sz]ation|monoclonal antibody|\bmAb\b", text, re.I))


def fetch_clinicaltrials(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    ctg = cfg["clinicaltrials"]
    base_url = ctg["base_url"]
    page_size = int(ctg.get("page_size", 100))
    max_pages = int(ctg.get("max_pages_per_disease", 5))
    out: List[Dict[str, Any]] = []
    for disease in ctg.get("diseases", []):
        query = ctg["query_template"].format(condition=disease["condition"])
        token: Optional[str] = None
        for page in range(max_pages):
            params = {"query.term": query, "pageSize": page_size, "format": "json"}
            if token:
                params["pageToken"] = token
            url = f"{base_url}?{urlencode(params)}"
            try:
                r = requests.get(url, timeout=45)
                r.raise_for_status()
                payload = r.json()
            except Exception as e:
                log(f"ClinicalTrials.gov fetch failed for {disease['label']}: {e}")
                # In local/offline environments, DNS failures can otherwise repeat for every disease.
                # On GitHub Actions this branch should normally not be reached.
                if any(token in str(e) for token in ["Failed to resolve", "NameResolutionError", "Temporary failure in name resolution"]):
                    log("Network/DNS unavailable; skipping remaining ClinicalTrials.gov queries for this run.")
                    return out
                break
            for study in payload.get("studies", []) or []:
                rec = extract_study(study, disease)
                if is_likely_vaccine_study(rec):
                    out.append(rec)
            token = payload.get("nextPageToken")
            if not token:
                break
        log(f"ClinicalTrials.gov: {disease['label']} -> {len([r for r in out if r['disease_id']==disease['id']])} study records")
    # Deduplicate exact NCT + disease duplicate searches
    seen = set()
    dedup = []
    for rec in out:
        key = (rec.get("study_id"), rec.get("disease_id"), rec.get("candidate_key"))
        if key not in seen:
            seen.add(key)
            dedup.append(rec)
    return dedup


def aggregate_candidate_records(studies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for s in studies:
        key = (s.get("disease_id", "unknown"), s.get("candidate_key") or norm_key(s.get("candidate", "unknown")))
        groups[key].append(s)
    candidates = []
    for (disease_id, ckey), rows in groups.items():
        stages = [r.get("stage", "Unknown") for r in rows]
        latest_stage = max_stage(stages)
        latest_update = max([r.get("last_update", "") for r in rows if r.get("last_update")] or [""])
        names = Counter(r.get("candidate") for r in rows if r.get("candidate"))
        candidate = names.most_common(1)[0][0] if names else "Unspecified vaccine candidate"
        disease = Counter(r.get("disease") for r in rows if r.get("disease")).most_common(1)[0][0]
        sponsors = sorted({r.get("lead_sponsor") for r in rows if r.get("lead_sponsor")})
        platforms = [r.get("platform") for r in rows if r.get("platform") and r.get("platform") != "Unclassified"]
        platform = Counter(platforms).most_common(1)[0][0] if platforms else "Unclassified"
        countries = sorted({c for r in rows for c in r.get("countries", []) if c})
        statuses = Counter(r.get("overall_status") for r in rows if r.get("overall_status"))
        is_active = any(r.get("overall_status") in ACTIVE_STATUSES for r in rows)
        is_halted = any(r.get("overall_status") in TERMINAL_STATUSES for r in rows)
        candidates.append({
            "id": make_id("CTG", [disease_id, ckey]),
            "candidate": candidate,
            "disease_id": disease_id,
            "disease": disease,
            "disease_category": rows[0].get("disease_category", ""),
            "stage": latest_stage,
            "stage_order": STAGE_ORDER.get(latest_stage, -1),
            "platform": platform,
            "developer": "; ".join(sponsors[:4]) or "Unknown",
            "sponsors": sponsors,
            "countries": countries,
            "status": "Active" if is_active else ("Halted/terminated" if is_halted else "Other/unknown"),
            "last_update": latest_update,
            "source": "ClinicalTrials.gov",
            "source_url": rows[0].get("source_url", ""),
            "supporting_trial_count": len(rows),
            "supporting_trials": sorted({r.get("study_id") for r in rows if r.get("study_id")}),
            "notes": "Candidate-level grouping is inferred from intervention names in trial records.",
        })
    return candidates


def first_existing_col(df: pd.DataFrame, names: List[str]) -> Optional[str]:
    norm_cols = {norm_key(c): c for c in df.columns}
    for name in names:
        nk = norm_key(name)
        if nk in norm_cols:
            return norm_cols[nk]
    # fuzzy contains
    for c in df.columns:
        ck = norm_key(c)
        for name in names:
            if norm_key(name) in ck or ck in norm_key(name):
                return c
    return None


def load_tables_from_url(url: str) -> List[pd.DataFrame]:
    try:
        if url.lower().endswith((".xlsx", ".xls")):
            xls = pd.ExcelFile(url)
            return [pd.read_excel(xls, sheet_name=s) for s in xls.sheet_names]
        return pd.read_html(url)
    except Exception as e:
        log(f"Table fetch failed for {url}: {e}")
        return []


def records_from_table(df: pd.DataFrame, source_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    if df.empty or len(df.columns) < 2:
        return []
    df = df.copy()
    df.columns = [clean_text(c) or f"col_{i}" for i, c in enumerate(df.columns)]
    cand_col = first_existing_col(df, ["candidate", "vaccine candidate", "product", "vaccine", "name", "candidate name"])
    stage_col = first_existing_col(df, ["stage", "phase", "latest stage", "development stage", "clinical phase"])
    platform_col = first_existing_col(df, ["platform", "technology", "vaccine platform", "approach", "type"])
    dev_col = first_existing_col(df, ["developer", "sponsor", "manufacturer", "developers", "lead developer", "institution"])
    disease_col = first_existing_col(df, ["disease", "pathogen", "target", "indication", "target pathogen"])
    if not cand_col:
        return []
    rows = []
    for _, row in df.iterrows():
        candidate = clean_text(row.get(cand_col))
        if not candidate or candidate.lower() in {"nan", "candidate"}:
            continue
        stage_raw = clean_text(row.get(stage_col)) if stage_col else source_cfg.get("default_stage", "Unknown")
        disease = clean_text(row.get(disease_col)) if disease_col else source_cfg.get("disease", "Unknown")
        if not disease:
            disease = source_cfg.get("disease", "Unknown")
        platform_raw = clean_text(row.get(platform_col)) if platform_col else ""
        developer = clean_text(row.get(dev_col)) if dev_col else ""
        stage = normalize_stage(stage_raw or source_cfg.get("default_stage", "Unknown"))
        platform = platform_raw or classify_platform(" ".join([candidate, clean_text(row.to_dict())]))
        rows.append({
            "id": make_id(source_cfg.get("id_prefix", "SRC"), [source_cfg.get("name", "source"), disease, candidate, stage]),
            "candidate": candidate,
            "disease_id": norm_key(disease).replace(" ", "_") or source_cfg.get("disease_id", "unknown"),
            "disease": disease,
            "disease_category": source_cfg.get("category", ""),
            "stage": stage,
            "stage_order": STAGE_ORDER.get(stage, -1),
            "platform": platform if platform else "Unclassified",
            "developer": developer or "Unknown",
            "sponsors": [developer] if developer else [],
            "countries": [],
            "status": "Curated source",
            "last_update": source_cfg.get("as_of", ""),
            "source": source_cfg.get("name", "Curated source"),
            "source_url": source_cfg.get("url", ""),
            "supporting_trial_count": 0,
            "supporting_trials": [],
            "notes": f"Best-effort parsed from {source_cfg.get('name', 'curated source')} table. Please review source formatting.",
        })
    return rows


def fetch_curated_tables(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for src in cfg.get("automated_curated_sources", []):
        if not src.get("enabled", True):
            continue
        before = len(out)
        parser = src.get("parser")
        if parser == "cidrap_landscape":
            out.extend(records_from_cidrap_landscape(src))
            log(f"Curated source: {src.get('name')} -> {len(out)-before} candidate records")
            continue
        if parser == "iavi_pipeline":
            out.extend(records_from_iavi_pipeline(src))
            log(f"Curated source: {src.get('name')} -> {len(out)-before} candidate records")
            continue
        url = src.get("url", "")
        tables = load_tables_from_url(url)
        for df in tables:
            rows = records_from_table(df, src)
            # keep plausible vaccine rows only
            for r in rows:
                joined = " ".join([r.get("candidate", ""), r.get("platform", ""), r.get("disease", "")])
                if re.search(r"vaccine|BCG|mRNA|protein|viral|vector|attenuated|inactivated|subunit|conjugate|VLP|RSV|GBS|TB|tuberculosis|mpox|smallpox|coronavirus|SARS|MERS|influenza", joined, re.I):
                    out.append(r)
        log(f"Curated source: {src.get('name')} -> {len(out)-before} candidate records")
    # Deduplicate curated rows
    seen = set()
    dedup = []
    for rec in out:
        key = (rec.get("source"), norm_key(rec.get("disease", "")), norm_key(rec.get("candidate", "")), rec.get("stage"))
        if key not in seen:
            seen.add(key)
            dedup.append(rec)
    return dedup


def records_from_cidrap_landscape(source_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Best-effort parser for CIDRAP vaccine technology landscapes."""
    url = source_cfg.get("url", "")
    try:
        html = requests.get(url, timeout=40, headers={"User-Agent": "vaccine-pipeline-dashboard/1.0"}).text
    except Exception as e:
        log(f"CIDRAP landscape fetch failed: {e}")
        return []
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"\n+", "\n", text)
    records: List[Dict[str, Any]] = []
    for m in re.finditer(r"\n([^\n]{2,100})\n\s*Print\s*\n\s*Vaccine Candidate Overview(.{0,1800}?)(?=\n[^\n]{2,100}\n\s*Print\s*\n\s*Vaccine Candidate Overview|\Z)", text, flags=re.S):
        candidate = clean_text(m.group(1))
        block = m.group(2)
        if not candidate or candidate.lower() in {"more detail+", "dev", "vaccine candidate overview"}:
            continue
        phase_m = re.search(r"(?:Phase|Stage)\s*\n\s*([^\n]+)", block, flags=re.I)
        platform_m = re.search(r"(?:Vaccine Platform|Platform|Technology)\s*\n\s*([^\n]+)", block, flags=re.I)
        status_m = re.search(r"R&D Status\s*\n\s*([^\n]+)", block, flags=re.I)
        dev_m = re.search(r"(?:Dev|Developer|Developers)\s*\n\s*([^\n]+)", block, flags=re.I)
        stage = normalize_stage(phase_m.group(1) if phase_m else source_cfg.get("default_stage", "Unknown"))
        platform = clean_text(platform_m.group(1)) if platform_m else classify_platform(candidate)
        developer = clean_text(dev_m.group(1)) if dev_m else "Unknown"
        if stage == "Unknown" and not platform:
            continue
        records.append({
            "id": make_id(source_cfg.get("id_prefix", "CIDRAP"), [candidate, stage, platform]),
            "candidate": candidate,
            "disease_id": source_cfg.get("disease_id", "unknown"),
            "disease": source_cfg.get("disease", "Unknown"),
            "disease_category": source_cfg.get("category", ""),
            "stage": stage,
            "stage_order": STAGE_ORDER.get(stage, -1),
            "platform": platform or "Unclassified",
            "developer": developer or "Unknown",
            "sponsors": [developer] if developer and developer != "Unknown" else [],
            "countries": [],
            "status": clean_text(status_m.group(1)) if status_m else "CIDRAP landscape candidate",
            "last_update": datetime.now(timezone.utc).date().isoformat(),
            "source": source_cfg.get("name", "CIDRAP landscape"),
            "source_url": url,
            "supporting_trial_count": 0,
            "supporting_trials": [],
            "notes": f"Best-effort parsed from {source_cfg.get('name', 'CIDRAP landscape')}.",
        })
    seen = set(); out = []
    for r in records:
        key = (norm_key(r["candidate"]), r["stage"], norm_key(r["platform"]))
        if key not in seen:
            seen.add(key); out.append(r)
    return out[:300]


def records_from_iavi_pipeline(source_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Best-effort parser for IAVI's static pipeline page."""
    url = source_cfg.get("url", "https://www.iavi.org/iavi-pipeline/")
    try:
        html = requests.get(url, timeout=40, headers={"User-Agent": "vaccine-pipeline-dashboard/1.0"}).text
    except Exception as e:
        log(f"IAVI pipeline fetch failed: {e}")
        return []
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<h3[^>]*>", "\n### ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"\n+", "\n", text)
    records=[]
    for m in re.finditer(r"###\s*([^\n]+)(.{0,1600}?)(?=\n###\s*[^\n]+|\Z)", text, flags=re.S):
        candidate = clean_text(m.group(1))
        block = m.group(2)
        if not candidate or candidate.lower() in {"partners"}:
            continue
        phase_m = re.search(r"(Pre-clinical|Preclinical|Phase\s*1|Phase\s*2|Phase\s*3|Phase\s*4)", block, flags=re.I)
        if not phase_m:
            continue
        disease = source_cfg.get("disease", "Priority infectious diseases")
        for d in ["HIV", "Sudan virus", "Lassa virus", "Marburg virus", "Tuberculosis"]:
            if re.search(re.escape(d), block, re.I):
                disease = d
                break
        partners_m = re.search(r"Partners\s*\n(.{0,350}?)(?:Pre-clinical|Preclinical|Phase\s*\d)", block, flags=re.I|re.S)
        developer = clean_text(partners_m.group(1)) if partners_m else "IAVI and partners"
        stage = normalize_stage(phase_m.group(1))
        records.append({
            "id": make_id(source_cfg.get("id_prefix", "IAVI"), [candidate, disease, stage]),
            "candidate": candidate,
            "disease_id": norm_key(disease).replace(" ", "_"),
            "disease": disease,
            "disease_category": source_cfg.get("category", "global_health"),
            "stage": stage,
            "stage_order": STAGE_ORDER.get(stage, -1),
            "platform": classify_platform(candidate) or "Unclassified",
            "developer": developer or "IAVI and partners",
            "sponsors": [developer] if developer else ["IAVI and partners"],
            "countries": [],
            "status": "IAVI pipeline candidate",
            "last_update": datetime.now(timezone.utc).date().isoformat(),
            "source": source_cfg.get("name", "IAVI Pipeline"),
            "source_url": url,
            "supporting_trial_count": 0,
            "supporting_trials": [],
            "notes": "Best-effort parsed from IAVI pipeline page.",
        })
    return records


def load_public_seed_records() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Load real public-source seed records when the runner has no network.

    This avoids reverting the public dashboard to artificial example rows. The seed
    file is intentionally limited and should be replaced by automated fetch output
    whenever network access is available.
    """
    seed_path = DATA_DIR / "pipeline.json"
    if seed_path.exists():
        try:
            candidates = json.loads(seed_path.read_text(encoding="utf-8"))
            if candidates and not any(str(r.get("source", "")).lower().startswith("sample") for r in candidates):
                studies = json.loads((DATA_DIR / "studies.json").read_text(encoding="utf-8")) if (DATA_DIR / "studies.json").exists() else []
                log(f"Loaded {len(candidates)} public-source seed records from data/pipeline.json")
                return candidates, studies
        except Exception as e:
            log(f"Public seed load failed: {e}")
    return [], []


# --- AI-assisted intelligence layer -------------------------------------------------
# The dashboard is static and runs on GitHub Actions, so this layer uses transparent
# deterministic heuristics that are safe to run without sending data to external AI
# services. It is labelled "AI-assisted" because it performs the first-stage tasks
# usually needed before LLM use: entity normalization, candidate matching, stage
# classification, and natural-language update summaries. A future version can swap
# these functions for an LLM-backed classifier while preserving the output schema.

CANDIDATE_ALIAS_RULES = [
    (r"\bComirnaty\b|BNT162b2|Pfizer[- ]BioNTech", "BNT162b2 / Comirnaty"),
    (r"\bSpikevax\b|mRNA[- ]1273", "mRNA-1273 / Spikevax"),
    (r"NVX[- ]CoV2373|Nuvaxovid|Novavax", "NVX-CoV2373 / Nuvaxovid"),
    (r"AZD1222|ChAdOx1 nCoV[- ]19|Vaxzevria", "AZD1222 / ChAdOx1 nCoV-19"),
    (r"Ad26\.COV2\.S|Janssen", "Ad26.COV2.S"),
    (r"CoronaVac", "CoronaVac"),
    (r"BBIBP[- ]CorV|Sinopharm", "BBIBP-CorV / Sinopharm"),
    (r"M72[/\- ]AS01E|M72", "M72/AS01E"),
    (r"BCG", "BCG"),
    (r"MTBVAC", "MTBVAC"),
    (r"VPM1002", "VPM1002"),
    (r"RTS,S|Mosquirix", "RTS,S/AS01"),
    (r"R21[/\- ]Matrix[- ]M|R21", "R21/Matrix-M"),
    (r"mRNA[- ]1345", "mRNA-1345"),
    (r"RSVpreF|Abrysvo", "RSVpreF / Abrysvo"),
    (r"RSVPreF3|Arexvy", "RSVPreF3 / Arexvy"),
    (r"nirsevimab|Beyfortus", "Nirsevimab / Beyfortus"),
]

STAGE_CONFIDENCE_BY_SOURCE = {
    "ClinicalTrials.gov": "high",
    "TB Vaccine Clinical Pipeline": "high",
    "TB Vaccine Preclinical Pipeline": "medium",
    "PATH RSV Clinical Trial Tracker": "high",
    "PATH GBS Vaccine Clinical Trial Tracker": "high",
    "CIDRAP Coronavirus Vaccine Technology Landscape": "medium",
    "CIDRAP Universal Influenza Vaccine Technology Landscape": "medium",
    "WHO Mpox Vaccine Tracker": "medium",
    "IAVI Pipeline": "medium",
}


def canonical_candidate_name(candidate: str) -> Tuple[str, str, str]:
    """Return canonical name, method, and confidence."""
    c = clean_text(candidate)
    if not c:
        return "Unspecified vaccine candidate", "AI-assisted normalization", "low"
    for pattern, canonical in CANDIDATE_ALIAS_RULES:
        if re.search(pattern, c, flags=re.I):
            return canonical, "AI-assisted alias rule", "high"
    # remove obvious dose/adjuvant/formulation noise but keep scientific identifiers
    cleaned = re.sub(r"\b(low|medium|high) dose\b|\bbooster\b|\bprime[- ]boost\b", " ", c, flags=re.I)
    cleaned = re.sub(r"\bwith placebo\b|\bversus placebo\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ;,/-")
    if not cleaned:
        cleaned = c
    confidence = "medium" if cleaned != c else "low"
    return cleaned, "AI-assisted text normalization", confidence


def candidate_similarity_key(candidate: str) -> str:
    name, _, _ = canonical_candidate_name(candidate)
    key = norm_key(name)
    key = re.sub(r"\b(vaccine|candidate|adjuvanted|recombinant|protein|subunit|mrna|rna|dna)\b", " ", key)
    return re.sub(r"\s+", " ", key).strip() or norm_key(name)


def annotate_stage_classification(rec: Dict[str, Any]) -> None:
    stage = normalize_stage(rec.get("stage"))
    source = clean_text(rec.get("source"))
    raw = " ".join([clean_text(rec.get("notes")), clean_text(rec.get("status")), clean_text(rec.get("stage"))])
    confidence = STAGE_CONFIDENCE_BY_SOURCE.get(source, "medium")
    method = "AI-assisted stage classification"
    rationale = "Stage normalized from public source text."
    if source == "ClinicalTrials.gov":
        method = "Registry phase mapping"
        rationale = "Stage mapped from ClinicalTrials.gov phase field."
    elif re.search(r"preclinical|pre[- ]clinical", raw, flags=re.I):
        rationale = "Stage inferred from preclinical pipeline/source wording."
    elif stage in {"Unknown", "Not Applicable"}:
        confidence = "low"
        rationale = "Source did not expose a clear development phase."
    rec["stage"] = stage
    rec["stage_order"] = STAGE_ORDER.get(stage, -1)
    rec["ai_stage_method"] = method
    rec["ai_stage_confidence"] = confidence
    rec["ai_stage_rationale"] = rationale


def merge_candidate_sources(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # AI-assisted candidate matching: merge likely equivalent candidates across sources
    # within the same disease area, preserving aliases and source provenance.
    for rec in records:
        canonical, method, conf = canonical_candidate_name(rec.get("candidate", ""))
        rec["canonical_candidate"] = canonical
        rec["candidate_match_method"] = method
        rec["candidate_match_confidence"] = conf
        rec["candidate_match_key"] = candidate_similarity_key(canonical)
        rec["candidate_aliases"] = sorted({clean_text(rec.get("candidate")), canonical} - {""})
        annotate_stage_classification(rec)

    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    source_rows = 0
    merged_rows = 0
    for rec in records:
        source_rows += 1
        key = (norm_key(rec.get("disease", "")), rec.get("candidate_match_key") or norm_key(rec.get("canonical_candidate", "")))
        if key not in merged:
            new_rec = dict(rec)
            new_rec["candidate"] = rec.get("canonical_candidate") or rec.get("candidate")
            new_rec["source_records"] = 1
            new_rec["sources_seen"] = [rec.get("source", "")]
            merged[key] = new_rec
            continue
        merged_rows += 1
        ex = merged[key]
        if STAGE_ORDER.get(rec.get("stage", "Unknown"), -1) > STAGE_ORDER.get(ex.get("stage", "Unknown"), -1):
            ex["stage"] = rec.get("stage", ex.get("stage"))
            ex["stage_order"] = STAGE_ORDER.get(ex["stage"], -1)
            ex["ai_stage_method"] = rec.get("ai_stage_method", ex.get("ai_stage_method"))
            ex["ai_stage_confidence"] = rec.get("ai_stage_confidence", ex.get("ai_stage_confidence"))
            ex["ai_stage_rationale"] = rec.get("ai_stage_rationale", ex.get("ai_stage_rationale"))
        ex["supporting_trial_count"] = int(ex.get("supporting_trial_count") or 0) + int(rec.get("supporting_trial_count") or 0)
        ex["supporting_trials"] = sorted(set(ex.get("supporting_trials", [])) | set(rec.get("supporting_trials", [])))
        ex["sponsors"] = sorted(set(ex.get("sponsors", [])) | set(rec.get("sponsors", [])))
        ex["countries"] = sorted(set(ex.get("countries", [])) | set(rec.get("countries", [])))
        ex["candidate_aliases"] = sorted(set(ex.get("candidate_aliases", [])) | set(rec.get("candidate_aliases", [])) | {rec.get("candidate", "")})
        ex["sources_seen"] = sorted(set(ex.get("sources_seen", [])) | {rec.get("source", "")})
        ex["source_records"] = int(ex.get("source_records") or 1) + 1
        if ex.get("developer") in {"", "Unknown"} and rec.get("developer"):
            ex["developer"] = rec.get("developer")
        ex["last_update"] = max(ex.get("last_update", ""), rec.get("last_update", ""))
        if ex.get("source") == "ClinicalTrials.gov" and rec.get("source") != "ClinicalTrials.gov":
            # Prefer a source URL from a curated landscape when the candidate is not tied to a single NCT record.
            ex["source"] = rec.get("source", ex.get("source"))
            ex["source_url"] = rec.get("source_url", ex.get("source_url"))
    out = sorted(merged.values(), key=lambda r: (-STAGE_ORDER.get(r.get("stage", "Unknown"), -1), r.get("disease", ""), r.get("candidate", "")))
    for r in out:
        r["ai_candidate_matching"] = "AI-assisted candidate matching applied"
        r["candidate_aliases"] = [a for a in r.get("candidate_aliases", []) if a and norm_key(a) != norm_key(r.get("candidate", ""))][:8]
    return out


def load_previous_candidates() -> List[Dict[str, Any]]:
    path = DATA_DIR / "pipeline.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def candidate_diff(previous: List[Dict[str, Any]], current: List[Dict[str, Any]]) -> Dict[str, Any]:
    def key(r: Dict[str, Any]) -> Tuple[str, str]:
        return (norm_key(r.get("disease", "")), candidate_similarity_key(r.get("candidate", "")))
    prev = {key(r): r for r in previous}
    curr = {key(r): r for r in current}
    new = [r for k, r in curr.items() if k not in prev]
    removed = [r for k, r in prev.items() if k not in curr]
    progressed = []
    changed = []
    for k, r in curr.items():
        if k in prev:
            old = prev[k]
            old_stage = normalize_stage(old.get("stage"))
            new_stage = normalize_stage(r.get("stage"))
            if STAGE_ORDER.get(new_stage, -1) > STAGE_ORDER.get(old_stage, -1):
                progressed.append({"candidate": r.get("candidate"), "disease": r.get("disease"), "from": old_stage, "to": new_stage})
            elif old_stage != new_stage:
                changed.append({"candidate": r.get("candidate"), "disease": r.get("disease"), "from": old_stage, "to": new_stage})
    return {"new_candidates": new[:20], "removed_candidates": removed[:20], "stage_progressions": progressed[:20], "stage_changes": changed[:20]}


def build_ai_weekly_summary(candidates: List[Dict[str, Any]], changes: Dict[str, Any]) -> List[str]:
    stage_counts = Counter(r.get("stage", "Unknown") for r in candidates)
    disease_counts = Counter(r.get("disease", "Unknown") for r in candidates)
    platform_counts = Counter(r.get("platform", "Unclassified") for r in candidates)
    bullets = []
    if changes.get("stage_progressions"):
        p = changes["stage_progressions"][0]
        bullets.append(f"{p['disease']}の{p['candidate']}が{p['from']}から{p['to']}へ進行しました。")
    if changes.get("new_candidates"):
        bullets.append(f"新規候補として{len(changes['new_candidates'])}件を検出しました。")
    if not bullets:
        bullets.append("前回データとの比較で明確なステージ進行は検出されませんでした。")
    top_disease = disease_counts.most_common(1)[0] if disease_counts else ("", 0)
    top_platform = platform_counts.most_common(1)[0] if platform_counts else ("", 0)
    bullets.append(f"候補数が最も多い疾患領域は{top_disease[0]}（{top_disease[1]}件）です。")
    bullets.append(f"最も多い技術分類は{top_platform[0]}（{top_platform[1]}件）です。")
    late = sum(stage_counts.get(s, 0) for s in ["Phase 3", "Phase 4", "Approved/Authorized"])
    bullets.append(f"Phase 3以降の候補は合計{late}件です。")
    return bullets

def make_summary(candidates: List[Dict[str, Any]], studies: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    previous = load_previous_candidates()
    changes = candidate_diff(previous, candidates)
    ai_bullets = build_ai_weekly_summary(candidates, changes)
    stage_counts = Counter(r.get("stage", "Unknown") for r in candidates)
    disease_counts = Counter(r.get("disease", "Unknown") for r in candidates)
    platform_counts = Counter(r.get("platform", "Unclassified") for r in candidates)
    conf_counts = Counter(r.get("ai_stage_confidence", "unknown") for r in candidates)
    match_conf_counts = Counter(r.get("candidate_match_confidence", "unknown") for r in candidates)
    alias_count = sum(len(r.get("candidate_aliases", [])) for r in candidates)
    preclinical = sum(1 for r in candidates if r.get("stage") in {"Discovery", "Preclinical"})
    clinical = sum(1 for r in candidates if STAGE_ORDER.get(r.get("stage", "Unknown"), -1) >= STAGE_ORDER["Phase 1"])
    recent = sorted([r for r in candidates if r.get("last_update")], key=lambda x: x.get("last_update", ""), reverse=True)[:30]
    sources = [{"name": "ClinicalTrials.gov API v2", "url": cfg["clinicaltrials"]["base_url"], "status": "automated"}]
    for s in cfg.get("automated_curated_sources", []):
        sources.append({"name": s.get("name"), "url": s.get("url"), "status": "best_effort_automated"})
    for s in cfg.get("manual_reference_sources", []):
        sources.append(s)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(candidates),
        "clinical_trial_record_count": len(studies),
        "preclinical_candidate_count": preclinical,
        "clinical_candidate_count": clinical,
        "disease_count": len(disease_counts),
        "platform_count": len(platform_counts),
        "stage_counts": dict(stage_counts),
        "disease_counts": dict(disease_counts),
        "platform_counts": dict(platform_counts),
        "recent_updates": recent,
        "sources": sources,
        "ai_intelligence": {
            "label": "AI支援型ワクチン開発動向トラッカー",
            "candidate_matching": {
                "method": "alias rules + normalized string matching",
                "matched_source_records": sum(max(0, int(r.get("source_records") or 1) - 1) for r in candidates),
                "aliases_detected": alias_count,
                "confidence_counts": dict(match_conf_counts),
            },
            "stage_classification": {
                "method": "registry phase mapping + rule-based public-source text classification",
                "confidence_counts": dict(conf_counts),
            },
            "weekly_summary": ai_bullets,
            "changes": changes,
        },
        "notes": [
            "ClinicalTrials.gov is trial-level; candidate-level grouping is inferred from intervention names.",
            "Preclinical candidates are obtained only from curated public pipeline tables where parsable; coverage is incomplete.",
            "AI-assisted components use transparent heuristic rules in GitHub Actions; classifications should be reviewed before policy use.",
        ],
    }

def fallback_samples() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    today = datetime.now(timezone.utc).date().isoformat()
    samples = [
        {"id":"SAMPLE-PRE-001","candidate":"Example preclinical coronavirus vaccine","disease_id":"coronavirus","disease":"Coronavirus","disease_category":"respiratory","stage":"Preclinical","stage_order":1,"platform":"Protein/subunit/VLP","developer":"Example university","sponsors":["Example university"],"countries":[],"status":"Sample","last_update":today,"source":"Sample data","source_url":"","supporting_trial_count":0,"supporting_trials":[],"notes":"Sample record"},
        {"id":"SAMPLE-P1-001","candidate":"Example RSV vaccine","disease_id":"rsv","disease":"RSV","disease_category":"respiratory","stage":"Phase 1","stage_order":3,"platform":"mRNA/RNA","developer":"Example biotech","sponsors":["Example biotech"],"countries":["United States"],"status":"Active","last_update":today,"source":"Sample data","source_url":"","supporting_trial_count":1,"supporting_trials":["NCT00000000"],"notes":"Sample record"},
        {"id":"SAMPLE-P3-001","candidate":"Example TB vaccine","disease_id":"tuberculosis","disease":"Tuberculosis","disease_category":"bacterial","stage":"Phase 3","stage_order":7,"platform":"Viral vector","developer":"Example PDP","sponsors":["Example PDP"],"countries":["South Africa","Kenya"],"status":"Active","last_update":today,"source":"Sample data","source_url":"","supporting_trial_count":2,"supporting_trials":["NCT11111111","NCT22222222"],"notes":"Sample record"},
    ]
    return samples, []


def write_outputs(candidates: List[Dict[str, Any]], studies: List[Dict[str, Any]], summary: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "pipeline.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA_DIR / "studies.json").write_text(json.dumps(studies, ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (DATA_DIR / "pipeline.csv").open("w", encoding="utf-8", newline="") as f:
        fields = ["id","candidate","candidate_aliases","disease","stage","ai_stage_confidence","platform","developer","status","last_update","source","source_url","supporting_trial_count","sources_seen"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in candidates:
            writer.writerow({k: clean_text(r.get(k)) for k in fields})


def main() -> int:
    cfg = load_config()
    studies = fetch_clinicaltrials(cfg)
    ctg_candidates = aggregate_candidate_records(studies)
    curated_candidates = fetch_curated_tables(cfg)
    candidates = merge_candidate_sources(ctg_candidates + curated_candidates)
    if not candidates:
        log("No records fetched from live sources; loading public-source seed records instead of artificial samples.")
        seed_candidates, seed_studies = load_public_seed_records()
        if seed_candidates:
            candidates, studies = merge_candidate_sources(seed_candidates), seed_studies
        else:
            log("No public seed file available; writing minimal sample records so the dashboard remains usable.")
            candidates, studies = fallback_samples()
    summary = make_summary(candidates, studies, cfg)
    write_outputs(candidates, studies, summary)
    log(f"Wrote {len(candidates)} candidate records and {len(studies)} supporting trial records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
