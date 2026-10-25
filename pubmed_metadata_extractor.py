import argparse
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Type
from pydantic import BaseModel, Field, ValidationError
import anthropic
import requests

# 1. Domain-specific extraction schemas
class BiomedicalMetadata(BaseModel):
    pathogen_name: Optional[str] = Field(None, description="The specific virus or bacteria strain studied.")
    genes_mentioned: List[str] = Field(default=[], description="List of specific genes or proteins discussed.")
    antigen_targets: List[str] = Field(default=[], description="Any identified vaccine antigens or immune targets.")
    methodology: str = Field(..., description="The main experimental or computational method used (e.g., RNA-seq, cryo-EM).")
    key_finding: str = Field(..., description="A one-sentence summary of the main scientific takeaway.")

class GeneralScienceMetadata(BaseModel):
    field_of_study: Optional[str] = Field(None, description="The scientific discipline or subfield this work belongs to.")
    key_concepts: List[str] = Field(default=[], description="Key concepts, techniques, or entities central to this work.")
    methodology: str = Field(..., description="The main experimental, computational, or theoretical approach used.")
    key_finding: str = Field(..., description="A one-sentence summary of the main takeaway.")

DOMAIN_SCHEMAS = {
    "biomedical": {
        "model": BiomedicalMetadata,
        "tool_name": "biomedical_extractor",
        "system_prompt": (
            "You are an expert biopharma data extraction assistant. You must extract and populate "
            "every single requested schema field precisely. If a field like methodology is not "
            "explicitly named, infer the high-level scientific approach from the context."
        ),
    },
    "general": {
        "model": GeneralScienceMetadata,
        "tool_name": "general_science_extractor",
        "system_prompt": (
            "You are an expert scientific literature analyst covering any field of study. You "
            "must extract and populate every single requested schema field precisely. If a field "
            "is not explicitly named, infer it from context."
        ),
    },
}

# 2. Default batch input/output folders on personal OneDrive, used only when
# --input_dir / --output_dir are not passed on the command line (e.g. when run
# standalone rather than staged by Nextflow).
DEFAULT_INPUT_DIR = Path(r"C:\Users\jagsc\OneDrive\abstracts_input")
DEFAULT_OUTPUT_DIR = Path(r"C:\Users\jagsc\OneDrive\metadata_output")

PUBMED_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
PUBMED_BATCH_SIZE = 200  # NCBI's recommended ceiling per efetch call without using the history server
NCBI_RATE_LIMIT_DELAY = 0.4  # seconds between requests, keeps us under the unauthenticated 3 req/sec cap

class ExtractionRefused(Exception):
    """Raised when Claude's safety classifier stops generation (stop_reason == 'refusal')."""

def slugify(text: str, max_length: int = 50) -> str:
    """Turns a search topic into a filesystem-safe filename fragment."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
    return (slug[:max_length] or "query")

def get_pubmed_total_count(topic: str) -> int:
    """Returns PubMed's true total match count for `topic`, independent of any retrieval cap."""
    resp = requests.get(PUBMED_ESEARCH_URL, params={
        "db": "pubmed",
        "term": topic,
        "retmode": "json",
        "retmax": 0,
    }, timeout=30)
    resp.raise_for_status()
    return int(resp.json().get("esearchresult", {}).get("count", 0))

def _parse_pubmed_article(article) -> Optional[tuple]:
    """Extracts (pmid, title, abstract_text) from one <PubmedArticle> element, or None if it has no abstract."""
    pmid_el = article.find(".//PMID")
    title_el = article.find(".//ArticleTitle")
    abstract_parts = [p for p in article.findall(".//Abstract/AbstractText") if p.text]

    if pmid_el is None or not abstract_parts:
        return None

    pmid = pmid_el.text
    title = (title_el.text or "").strip() if title_el is not None else ""
    abstract_text = "\n".join(part.text.strip() for part in abstract_parts)
    return pmid, title, abstract_text

def fetch_pubmed_abstracts(topic: str, max_results: int, input_dir: Path) -> List[Path]:
    """Searches PubMed for `topic` and writes up to max_results abstracts as .txt files into
    input_dir, returning only the paths fetched for this specific query. Paginates in batches
    of PUBMED_BATCH_SIZE via retstart when max_results exceeds a single batch.

    Note: PubMed only indexes biomedical literature, so this retrieval step returns
    biomedical-adjacent results regardless of which --domain schema is selected.
    """
    total_count = get_pubmed_total_count(topic)
    print(f"PubMed reports {total_count} total match(es) for topic: {topic!r}")

    if total_count == 0:
        return []

    to_fetch = min(max_results, total_count)
    if max_results < total_count:
        print(f"Fetching the first {to_fetch} of {total_count} match(es) (raise --max_results to get more).")
    else:
        print(f"Fetching all {to_fetch} match(es).")

    input_dir.mkdir(parents=True, exist_ok=True)
    fetched_paths = []
    retstart = 0

    while retstart < to_fetch:
        batch_size = min(PUBMED_BATCH_SIZE, to_fetch - retstart)

        time.sleep(NCBI_RATE_LIMIT_DELAY)
        search_resp = requests.get(PUBMED_ESEARCH_URL, params={
            "db": "pubmed",
            "term": topic,
            "retstart": retstart,
            "retmax": batch_size,
            "retmode": "json",
        }, timeout=30)
        search_resp.raise_for_status()
        pmids = search_resp.json().get("esearchresult", {}).get("idlist", [])

        if not pmids:
            break

        time.sleep(NCBI_RATE_LIMIT_DELAY)
        fetch_resp = requests.get(PUBMED_EFETCH_URL, params={
            "db": "pubmed",
            "id": ",".join(pmids),
            "rettype": "abstract",
            "retmode": "xml",
        }, timeout=30)
        fetch_resp.raise_for_status()

        root = ET.fromstring(fetch_resp.content)
        for article in root.findall(".//PubmedArticle"):
            parsed = _parse_pubmed_article(article)
            if parsed is None:
                continue
            pmid, title, abstract_text = parsed

            out_path = input_dir / f"pubmed_{pmid}.txt"
            out_path.write_text(f"{title}\n\n{abstract_text}", encoding="utf-8")
            fetched_paths.append(out_path)
            print(f"Saved PubMed ID {pmid}: {title[:80]}")

        retstart += len(pmids)

    return fetched_paths

def extract_metadata(abstract_text: str, domain: str) -> BaseModel:
    """Uses Claude to pull structured fields out of the abstract text for the given domain, validated via Pydantic."""
    domain_config = DOMAIN_SCHEMAS[domain]
    schema_model: Type[BaseModel] = domain_config["model"]

    client = anthropic.Anthropic()  # Automatically reads ANTHROPIC_API_KEY from environment variables

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=2048,
        system=domain_config["system_prompt"],
        messages=[{"role": "user", "content": f"Extract metadata from this abstract: {abstract_text}"}],
        tools=[{
            "name": domain_config["tool_name"],
            "description": "Extract structured entities from scientific literature",
            "input_schema": schema_model.model_json_schema()
        }],
        tool_choice={"type": "tool", "name": domain_config["tool_name"]}
    )

    if response.stop_reason == "refusal":
        raise ExtractionRefused(
            "Claude's safety classifier stopped generation partway through this abstract."
        )

    # Parse and strictly validate the structure via Pydantic
    tool_input = response.content[0].input
    return schema_model(**tool_input)

def process_abstracts(abstract_files: List[Path], output_dir: Path, domain: str, output_filename: str):
    """Extracts validated metadata for each abstract and writes all results into a single
    consolidated JSON file (one record per successfully-extracted abstract)."""
    output_dir.mkdir(parents=True, exist_ok=True)

    if not abstract_files:
        print("No abstracts to process.")
        return

    records = []
    for abstract_path in abstract_files:
        print(f"\nProcessing {abstract_path.name}...")
        abstract_text = abstract_path.read_text(encoding="utf-8")

        try:
            validated_metadata = extract_metadata(abstract_text, domain)
        except ExtractionRefused as e:
            print(f"Warning: Claude declined to process {abstract_path.name} (safety refusal), skipping. Details: {e}")
            continue
        except ValidationError as e:
            print(f"Warning: validation failed for {abstract_path.name}, skipping. Details: {e}")
            continue
        except Exception as e:
            print(f"Warning: extraction failed for {abstract_path.name}, skipping. Details: {e}")
            continue

        records.append({
            "source_file": abstract_path.name,
            **validated_metadata.model_dump()
        })

    output_path = output_dir / output_filename
    output_path.write_text(json.dumps(records, indent=4), encoding="utf-8")
    print(f"\nSuccess! Wrote {len(records)} validated record(s) to: {output_path}")

def parse_args():
    parser = argparse.ArgumentParser(
        description="Batch-extract structured metadata from scientific abstracts using Claude."
    )
    parser.add_argument(
        "--input_dir", type=Path, default=DEFAULT_INPUT_DIR,
        help="Directory containing .txt abstracts to process when --topic is not set "
             "(default: OneDrive abstracts_input)."
    )
    parser.add_argument(
        "--output_dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help="Directory to write the consolidated metadata JSON file (default: OneDrive metadata_output)."
    )
    parser.add_argument(
        "--domain", choices=sorted(DOMAIN_SCHEMAS.keys()), default="biomedical",
        help="Which extraction schema to use."
    )
    parser.add_argument(
        "--topic", type=str, default=None,
        help="If set, searches PubMed for this topic, downloads matching abstracts into "
             "--input_dir, and processes only those newly-fetched abstracts (rather than "
             "the whole --input_dir backlog)."
    )
    parser.add_argument(
        "--max_results", type=int, default=5,
        help="Max PubMed abstracts to fetch when --topic is set (default: 5)."
    )
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if args.topic:
        print(f"Searching PubMed for topic: {args.topic!r} (max {args.max_results} results)...")
        fetched_files = fetch_pubmed_abstracts(args.topic, args.max_results, args.input_dir)
        output_filename = f"{slugify(args.topic)}_{timestamp}_metadata.json"
        process_abstracts(fetched_files, args.output_dir, args.domain, output_filename)
    else:
        abstract_files = sorted(args.input_dir.glob("*.txt"))
        output_filename = f"batch_{timestamp}_metadata.json"
        process_abstracts(abstract_files, args.output_dir, args.domain, output_filename)
