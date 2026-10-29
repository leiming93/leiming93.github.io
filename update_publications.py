#!/usr/bin/env python3
"""Sync public ORCID works into this site's existing HTML. Python 3.11+, stdlib only."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

START = "<!-- ORCID-PUBLICATIONS:START -->"
END = "<!-- ORCID-PUBLICATIONS:END -->"
ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("title", "authors", "journal", "year", "volume", "issue", "pages", "type")


def plain(value):
    """Decode the simple LaTeX commonly present in exported ORCID BibTeX."""
    text = html.unescape(str(value or ""))
    accents = {"'": "\u0301", '`': "\u0300", '^': "\u0302", '"': "\u0308", '~': "\u0303", '=': "\u0304"}
    text = re.sub(r'\\([\'`^"~=])\s*\{?([A-Za-z])\}?',
                  lambda m: unicodedata.normalize("NFC", m[2] + accents[m[1]]), text)
    for command, replacement in {r"\&": "&", r"\%": "%", r"\_": "_", r"\#": "#", r"\textendash": "–", r"\textemdash": "—"}.items():
        text = text.replace(command, replacement)
    text = re.sub(r"\\(?:textit|textbf|emph|mathrm)\s*", "", text)
    return " ".join(text.replace("{", "").replace("}", "").replace("---", "—").replace("--", "–").split())


def title_key(text):
    text = unicodedata.normalize("NFKD", plain(text)).casefold()
    words = re.findall(r"[^\W_]+", "".join(c for c in text if not unicodedata.combining(c)))
    return "".join(w for w in words if w not in {"a", "an", "the"})


def normalize_doi(value):
    text = unquote(str(value or "").strip())
    text = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", text, flags=re.I)
    return text.lower() if re.fullmatch(r"10\.\d{4,9}/\S+", text) else ""


def safe_url(value):
    text = str(value or "").strip()
    parsed = urlparse(text)
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def bibtex_fields(citation):
    """Read fields from one BibTeX entry, respecting nested braces and quotes."""
    text = (citation or {}).get("citation-value") or ""
    if (citation or {}).get("citation-type", "").lower() != "bibtex":
        return {}
    fields, i = {}, text.find(",") + 1
    while 0 < i < len(text):
        match = re.search(r"\b([A-Za-z][\w-]*)\s*=\s*", text[i:])
        if not match:
            break
        key = match[1].lower()
        i += match.end()
        start = i
        if i < len(text) and text[i] in '{"':
            opening = text[i]
            closing = "}" if opening == "{" else '"'
            depth, i, start = 1, i + 1, i + 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                    continue
                if opening == "{" and text[i] == "{":
                    depth += 1
                if text[i] == closing:
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            fields[key] = plain(text[start:i])
            i += 1
        else:
            while i < len(text) and text[i] not in ",}\n":
                i += 1
            fields[key] = plain(text[start:i])
    return fields


def display_name(value):
    name = plain(value)
    parts = [p.strip() for p in name.split(",")]
    if len(parts) == 2:
        return f"{parts[1]} {parts[0]}".strip()
    if len(parts) == 3:
        return f"{parts[2]} {parts[0]}, {parts[1]}".strip()
    return name


def get_json(url, attempts=3):
    is_orcid = urlparse(url).hostname == "pub.orcid.org"
    headers = {"Accept": "application/vnd.orcid+json" if is_orcid else "application/json",
               "User-Agent": "Ming-Lei-Publications/1.0 (https://leiming93.github.io/)"}
    token = os.environ.get("ORCID_ACCESS_TOKEN", "")
    if is_orcid and token:
        headers["Authorization"] = "Bearer " + token
    for attempt in range(attempts):
        try:
            with urlopen(Request(url, headers=headers), timeout=25) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError("Expected a JSON object from the publication service")
            return result
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            if isinstance(error, HTTPError) and error.code not in {408, 429, 500, 502, 503, 504}:
                raise
            if attempt + 1 == attempts:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Request failed")


def read_public_works(orcid):
    if not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", orcid):
        raise ValueError("Invalid ORCID iD in data/publications.json")
    base = f"https://pub.orcid.org/v3.0/{orcid}"
    summary = get_json(base + "/works")
    groups = summary.get("group")
    if not isinstance(groups, list) or not groups:
        raise ValueError("ORCID returned no public works; existing files have been preserved")
    selected, groups_by_code = [], {}
    for group in groups:
        summaries = group.get("work-summary") or []
        public = [w for w in summaries if w.get("visibility") == "public"]
        if not public:
            raise ValueError("Unexpected non-public or empty work group in public API")
        preferred = max(public, key=lambda w: int(w.get("display-index") or 0))
        code = int(preferred["put-code"])
        selected.append(code)
        groups_by_code[code] = public
    results = []
    for start in range(0, len(selected), 50):
        requested = selected[start:start + 50]
        response = get_json(base + "/works/" + ",".join(map(str, requested)))
        records = [item.get("work") for item in (response.get("bulk") or [])]
        if any(not w for w in records) or {w["put-code"] for w in records} != set(requested):
            raise ValueError("Incomplete ORCID response; existing files have been preserved")
        for work in records:
            if work.get("visibility") != "public":
                raise ValueError("A work is no longer public; retry the sync later")
            group = groups_by_code[work["put-code"]]
            work["_group_codes"] = [w["put-code"] for w in group]
            work["_group_ids"] = [e for w in group for e in ((w.get("external-ids") or {}).get("external-id") or [])]
        results.extend(records)
    return results


def publication_from_work(work):
    bib = bibtex_fields(work.get("citation"))
    contributors = (work.get("contributors") or {}).get("contributor") or []
    authors = [display_name((c.get("credit-name") or {}).get("value")) for c in contributors]
    authors = [name for name in authors if name]
    if not authors and bib.get("author"):
        authors = [display_name(name) for name in re.split(r"\s+and\s+", bib["author"])]
    date = work.get("publication-date") or {}
    year = (date.get("year") or {}).get("value") or bib.get("year", "")
    title = plain(((work.get("title") or {}).get("title") or {}).get("value") or bib.get("title"))
    if not title or not re.fullmatch(r"\d{4}", str(year)) or not authors:
        raise ValueError(f"Work {work.get('put-code')} has incomplete title/year/authors; review the ORCID entry")
    external = ((work.get("external-ids") or {}).get("external-id") or []) + work.get("_group_ids", [])
    dois = [normalize_doi(e.get("external-id-value")) for e in external
            if e.get("external-id-type", "").lower() == "doi" and e.get("external-id-relationship") in {None, "self"}]
    doi = next((d for d in dois if d), normalize_doi(bib.get("doi")))
    return {"id": f"orcid-{work['put-code']}", "title": title, "authors": authors,
            "journal": plain((work.get("journal-title") or {}).get("value") or bib.get("journal") or bib.get("booktitle")),
            "year": int(year), "volume": bib.get("volume", ""), "issue": bib.get("number", ""),
            "pages": bib.get("pages") or bib.get("eid", ""), "doi": doi,
            "url": safe_url((work.get("url") or {}).get("value") or bib.get("url")),
            "type": work.get("type"), "orcid_put_codes": work.get("_group_codes", [work["put-code"]]),
            "hidden": False, "overrides": {}}


def merge_publications(existing, incoming):
    result = copy.deepcopy(existing)
    for new in incoming:
        matches = []
        for old in result:
            same_code = set(old.get("orcid_put_codes", [])) & set(new["orcid_put_codes"])
            same_doi = bool(new.get("doi")) and normalize_doi(old.get("doi")) == new["doi"]
            same_title = title_key(old["title"]) == title_key(new["title"])
            if same_code or same_doi or same_title:
                matches.append(old)
        if len(matches) > 1:
            raise ValueError(f"Multiple existing entries match {new['title']}; merge those entries in the data file")
        if not matches:
            result.append(copy.deepcopy(new))
            continue
        old = matches[0]
        for field in FIELDS:
            if new.get(field):
                if field == "journal" and title_key(old.get(field)) == title_key(new[field]):
                    continue  # Preserve the site's journal capitalization.
                old[field] = copy.deepcopy(new[field])
        old["orcid_put_codes"] = sorted(set(old.get("orcid_put_codes", [])) | set(new["orcid_put_codes"]))
        for field in ("doi", "url"):
            if new.get(field):
                old[field] = new[field]
    return result


def effective(publication):
    result = dict(publication)
    result.update(publication.get("overrides") or {})
    return result


def crossref_match(publication, candidates):
    """Accept only a unique DOI with matching title, first author and year."""
    p = effective(publication)
    first = title_key(p["authors"][0])
    matching = {}
    for item in candidates:
        if not any(title_key(t) == title_key(p["title"]) for t in item.get("title", [])):
            continue
        authors = item.get("author") or []
        if not authors or title_key((authors[0].get("given", "") + " " + authors[0].get("family", "")).strip()) != first:
            continue
        years = set()
        for field in ("published", "published-online", "published-print", "issued"):
            for parts in (item.get(field) or {}).get("date-parts") or []:
                if parts:
                    years.add(int(parts[0]))
        if not years or min(abs(y - int(p["year"])) for y in years) > 1:
            continue
        doi = normalize_doi(item.get("DOI"))
        if doi:
            matching[doi] = item
    return next(iter(matching)) if len(matching) == 1 else ""


def enrich_missing_dois(publications):
    for publication in publications:
        p = effective(publication)
        if p.get("hidden") or normalize_doi(p.get("doi")):
            continue
        query = urlencode({"query.bibliographic": p["title"], "rows": 5})
        try:
            response = get_json("https://api.crossref.org/works?" + query, attempts=2)
            doi = crossref_match(publication, (response.get("message") or {}).get("items") or [])
            if doi:
                publication["doi"] = doi
                publication["doi_source"] = "Crossref: title, first author and year verified"
                print(f"Found DOI: {doi}")
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            # ORCID content remains usable when optional DOI enrichment is unavailable.
            print(f"WARNING: DOI lookup unavailable for {p['id']}: {type(error).__name__}", file=sys.stderr)


def render(publications, orcid, owner_name):
    shown = [effective(p) for p in publications if not effective(p).get("hidden")]
    if not shown:
        raise ValueError("Refusing to publish an empty list")
    ids = [p["id"] for p in shown]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate publication IDs")
    years = sorted({int(p["year"]) for p in shown}, reverse=True)
    esc = lambda text: html.escape(str(text or ""), quote=True)
    parts = ['<nav class="years" aria-label="论文年份">' + "".join(f'<a href="#year-{y}">{y}</a>' for y in years) + '</nav>']
    for year in years:
        parts.append(f'<section class="publication-year" id="year-{year}" aria-labelledby="heading-{year}"><div class="section-heading"><h2 id="heading-{year}">{year}</h2></div><ol class="research-references" aria-label="{year} 年论文">')
        # Keep the user's established order within each year. New records are appended.
        for p in (p for p in shown if int(p["year"]) == year):
            names = [f'<strong>{esc(n)}</strong>' if title_key(n) == title_key(owner_name) else esc(n) for n in p["authors"]]
            journal = f' <strong><em class="research-journal" lang="en">{esc(p.get("journal"))}</em></strong>' if p.get("journal") else ""
            volume = str(p.get("volume") or "")
            if p.get("issue"):
                volume += f'({p["issue"]})'
            citation = ", ".join(str(v) for v in (volume, p.get("pages")) if v)
            metadata = f" · {year}" + (f" · {esc(citation)}" if citation else "")
            doi = normalize_doi(p.get("doi"))
            url = "https://doi.org/" + quote(doi, safe="/:();._-") if doi else safe_url(p.get("url"))
            label = "论文原文" if doi else (p.get("url_label") or "论文信息")
            if not url:
                url, label = f"https://orcid.org/{orcid}", "ORCID记录"
            parts.append(f'<li class="research-reference" id="{esc(p["id"])}" data-paper-id="{esc(p["id"])}" aria-label="{esc(p["title"])}"><p><span lang="en">{", ".join(names)}</span>{journal}{metadata} <span class="research-paper-link"><a href="{esc(url)}" aria-label="{esc(label)}：{esc(p["title"])}">{esc(label)}</a></span></p></li>')
        parts.append("</ol></section>")
    return "\n".join(parts)


def update_html(document, section):
    if document.count(START) != 1 or document.count(END) != 1:
        raise ValueError("HTML sync markers are missing or duplicated; upload the supplied publications.html")
    before, rest = document.split(START)
    _, after = rest.split(END)
    return before + START + "\n" + section + "\n" + END + after


def atomic_write(path, content):
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def run(root=ROOT, offline=False, enrich=True):
    data_path, html_path = root / "data/publications.json", root / "publications.html"
    data = json.loads(data_path.read_text(encoding="utf-8"))
    document = html_path.read_text(encoding="utf-8")
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported publications data schema")
    if not offline:
        works = read_public_works(data["orcid"])
        accepted = set(data.get("include_types", ["journal-article", "conference-paper"]))
        incoming = [publication_from_work(w) for w in works if w.get("type") in accepted]
        if not incoming:
            raise ValueError("No supported papers were returned; existing files have been preserved")
        data["publications"] = merge_publications(data["publications"], incoming)
        if enrich:
            enrich_missing_dois(data["publications"])
        data["last_synced_at"] = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        data["last_orcid_work_count"] = len(works)
    section = render(data["publications"], data["orcid"], data.get("owner_name", "Ming Lei"))
    updated = update_html(document, section)
    # All requests, parsing and validation finish before either file is written.
    atomic_write(data_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    atomic_write(html_path, updated)
    visible = sum(not effective(p).get("hidden") for p in data["publications"])
    print(f"Ready: {visible} displayed papers; {data.get('last_orcid_work_count', 'cached')} public ORCID works.")
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Render the saved data without any network access")
    parser.add_argument("--no-crossref", action="store_true", help="Skip optional DOI lookups")
    args = parser.parse_args()
    try:
        run(offline=args.offline, enrich=not args.no_crossref)
    except Exception as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
