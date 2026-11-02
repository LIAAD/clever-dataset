"""Readers of the two datasets used by the baselines, CLEVER and ClaimPT.

CLEVER is read from a JSON file (a list of articles) or a JSONL file (one article per line).
The ClaimPT silver data is read from JSON files with one entry per claim, whose claim spans
carry their synthetic questions. The document-level split of CLEVER used by the baselines
(40 training / 100 test claim spans) is fixed in split/clever_split_ids_seed0.json.
"""

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

SPLIT = Path(__file__).resolve().parents[1] / "split" / "clever_split_ids_seed0.json"

PT_MONTHS = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
             "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}


def ler_json(caminho):
    """A JSON file, or a JSONL file as the list of its lines."""
    caminho = Path(caminho)
    if not caminho.exists():
        raise SystemExit(f"Falta {caminho}.")
    texto = caminho.read_text(encoding="utf-8")
    if caminho.suffix == ".jsonl":
        return [json.loads(linha) for linha in texto.splitlines() if linha.strip()]
    return json.loads(texto)


# --- ClaimPT ---------------------------------------------------------------------------------

def _clean(text):
    return re.sub(r"\s+", " ", str(text)).strip() if text is not None else ""


def _dedupe(items):
    seen, out = set(), []
    for item in items:
        item = _clean(item)
        if item and item.casefold() not in seen:
            seen.add(item.casefold())
            out.append(item)
    return out


def parse_date(value):
    """'04 dez 2023', '2023-12-04', ... -> ISO YYYY-MM-DD."""
    text = _clean(value).lower()
    if not text:
        return ""
    m = re.match(r"^(\d{1,2})\s+([a-zç]{3})\s+(\d{4})$", text)
    if m and PT_MONTHS.get(m.group(2)):
        return f"{int(m.group(3)):04d}-{PT_MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return _clean(value)


def extract_texts(value):
    """Texts of a field that may be a str, a {'text': ...} dict or a list of them."""
    if not value:
        return []
    if isinstance(value, str):
        return _dedupe([value])
    if isinstance(value, dict):
        return _dedupe([value.get("text", "")])
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(extract_texts(item))
        return _dedupe(out)
    return []


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def select_texts_within(value, span_begin, span_end):
    """Texts whose offsets lie inside the span; all of them if none does.

    Ties each claim object to its span while keeping claimers outside it.
    """
    items = value if isinstance(value, list) else [value]
    selected, all_texts, had_offsets = [], [], False
    for item in items:
        text = _clean(item.get("text", "")) if isinstance(item, dict) else _clean(item)
        if not text:
            continue
        all_texts.append(text)
        begin, end = (_as_int(item.get("begin")), _as_int(item.get("end"))) if isinstance(item, dict) else (None, None)
        if begin is not None and end is not None:
            had_offsets = True
            if span_begin is not None and span_end is not None and span_begin <= begin and end <= span_end:
                selected.append(text)
        else:
            selected.append(text)
    if not selected and had_offsets:
        selected = all_texts
    return _dedupe(selected)


def _span_id(document, begin, end, text):
    if begin is not None and end is not None:
        return f"{document}:{begin}:{end}"
    return f"{document}:{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}"


def iter_span_units(record, com_perguntas=False):
    """One dict per claim span of a ClaimPT entry with claim=true; with `com_perguntas`, the
    span's silver questions too."""
    if not (isinstance(record, dict) and record.get("claim") is True):
        return
    document = record.get("document", "")
    pub_time = parse_date(record.get("publication_time"))
    claim_time = " / ".join(extract_texts(record.get("Time")))
    object_raw = record.get("claim_object")
    claimer_raw = record.get("claimer")

    spans = record.get("claim_span")
    spans = spans if isinstance(spans, list) else [spans] if spans else []
    for span in spans:
        if not isinstance(span, dict):
            continue
        text = _clean(span.get("text"))
        if not text:
            continue
        begin, end = _as_int(span.get("begin")), _as_int(span.get("end"))
        unidade = {
            "id": _span_id(document, begin, end, text),
            "document": document,
            "publication_time": pub_time,
            "claim_time": claim_time,
            "claim_span": text,
            "span_begin": begin,
            "span_end": end,
            "claimers": select_texts_within(claimer_raw, begin, end),
            "claim_objects": select_texts_within(object_raw, begin, end),
        }
        if com_perguntas:
            unidade["questions"] = list(span.get("questions") or [])
        yield unidade


def unidades_silver(caminhos):
    """The ClaimPT claim spans with their silver questions, in the order of the files given."""
    return [u for caminho in caminhos for registo in ler_json(caminho)
            for u in iter_span_units(registo, com_perguntas=True)]
