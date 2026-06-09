"""
pdf_extractor.py
----------------
Extracts structured content (headers, text, tables) from a PDF
using pdfplumber. Headers are detected by bold font + numbered pattern.
Both pdfplumber-native tables and layout-gap-based "unstructured" tables
are captured, with bbox tracking to avoid duplication.

Usage:
    python pdf_extractor.py input.pdf output.json
    python pdf_extractor.py input.pdf               # outputs input.json
"""

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path

import pdfplumber

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------
# HEADER DETECTION
# -----------------------------------------------------------------------

def is_header_line(line_chars: list, threshold: float = 0.6) -> bool:
    """
    Return True if the line looks like a section header:
    - At least `threshold` fraction of characters use a bold font, AND
    - The text starts with a numbered pattern (e.g. '1.', '1.2', 'Section 3').

    Note: this heuristic is tuned for numbered/bold headers. PDFs that use
    colour, font-size, or all-caps for headings will need additional checks.
    """
    if not line_chars:
        return False

    text = "".join(c["text"] for c in line_chars).strip()
    if not text:
        return False

    bold_count = sum(
        1 for c in line_chars
        if "bold" in c.get("fontname", "").lower()
    )
    is_bold = (bold_count / len(line_chars)) >= threshold

    number_pattern = re.match(
        r'^(section\s*\d+|\d+\.\d+|\d+\.)',
        text,
        re.IGNORECASE,
    )

    return is_bold and bool(number_pattern)


# -----------------------------------------------------------------------
# BBOX OVERLAP
# -----------------------------------------------------------------------

def is_inside_bbox(line_top: float, line_bottom: float, bbox: dict) -> bool:
    """Return True if the line's vertical span overlaps with bbox."""
    return not (line_bottom < bbox["top"] or line_top > bbox["bottom"])


# -----------------------------------------------------------------------
# CHARACTER → LINE GROUPING
# -----------------------------------------------------------------------

def group_chars_to_lines(chars: list, y_tolerance: int = 3) -> list:
    """
    Bucket characters into lines by rounding their `top` coordinate,
    then sort each line left-to-right.
    """
    lines: dict = defaultdict(list)
    for char in chars:
        y_key = round(char["top"] / y_tolerance) * y_tolerance
        lines[y_key].append(char)

    return [
        sorted(line_chars, key=lambda c: c["x0"])
        for _, line_chars in sorted(lines.items())
    ]


def line_to_text(line_chars: list, x_tolerance: float = 2) -> str:
    """
    Join characters into a string, inserting a space wherever the gap
    between consecutive characters exceeds x_tolerance.
    """
    if not line_chars:
        return ""

    words = []
    current_word = line_chars[0]["text"]

    for prev_char, char in zip(line_chars, line_chars[1:]):
        gap = char["x0"] - prev_char["x1"]
        if gap < x_tolerance:
            current_word += char["text"]
        else:
            words.append(current_word)
            current_word = char["text"]

    words.append(current_word)
    return " ".join(words)


# -----------------------------------------------------------------------
# UNSTRUCTURED TABLE DETECTION
# -----------------------------------------------------------------------

def group_words_to_lines(words: list, y_tolerance: int = 3) -> list:
    """Same bucketing logic as group_chars_to_lines, but for word dicts."""
    lines: dict = defaultdict(list)
    for word in words:
        y_key = round(word["top"] / y_tolerance) * y_tolerance
        lines[y_key].append(word)

    return [
        sorted(line_words, key=lambda w: w["x0"])
        for _, line_words in sorted(lines.items())
    ]


def has_large_gap(line_words: list, gap_threshold: float = 12) -> tuple:
    """
    Detect whether a line of words contains a large horizontal gap —
    the signature of a key/value layout not captured by pdfplumber's
    table finder.

    Returns (has_gap: bool, split_index: int | None).
    """
    if len(line_words) < 2:
        return False, None

    gaps = [
        line_words[i + 1]["x0"] - line_words[i]["x1"]
        for i in range(len(line_words) - 1)
    ]
    max_gap = max(gaps)

    if max_gap > gap_threshold:
        return True, gaps.index(max_gap) + 1

    return False, None


def extract_unstructured_tables_with_bbox(words: list) -> list:
    """
    Scan lines for large gaps and group consecutive gap-containing lines
    into key/value table blocks, each with a bounding box.

    Returns a list of (table_rows, bbox) tuples where:
        table_rows = [{"key": ..., "value": ...}, ...]
        bbox       = {"top": ..., "bottom": ...}
    """
    lines = group_words_to_lines(words)
    gap_info = [(line, *has_large_gap(line)) for line in lines]

    tables = []
    current_table: list = []
    current_bbox: dict | None = None

    for line, has_gap, split_index in gap_info:
        if has_gap:
            left = " ".join(w["text"] for w in line[:split_index])
            right = " ".join(w["text"] for w in line[split_index:])
            is_valid = len(left) > 1 and len(right) > 1
        else:
            is_valid = False

        if is_valid:
            current_table.append({"key": left, "value": right})

            top = min(w["top"] for w in line)
            bottom = max(w["bottom"] for w in line)
            if current_bbox is None:
                current_bbox = {"top": top, "bottom": bottom}
            else:
                current_bbox["top"] = min(current_bbox["top"], top)
                current_bbox["bottom"] = max(current_bbox["bottom"], bottom)
        else:
            if current_table:
                tables.append((current_table, current_bbox))
                current_table = []
                current_bbox = None

    if current_table:
        tables.append((current_table, current_bbox))

    return tables


# -----------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------

def flush_text_buffer(buffer: list, section: dict) -> None:
    """Append accumulated text lines to the current section and clear the buffer."""
    if buffer and section is not None:
        section["content"].append({
            "type": "text",
            "data": " ".join(buffer),
        })
        buffer.clear()


def build_structured_table(raw_table: list) -> list:
    """
    Convert a pdfplumber raw table (list-of-lists) into a list of dicts
    keyed by the first row, replacing None headers with 'col_N'.
    Skips the header row itself.
    """
    if not raw_table:
        return []

    header = [
        (cell if cell else f"col_{i}")
        for i, cell in enumerate(raw_table[0])
    ]
    result = []
    for row in raw_table[1:]:
        row_dict = {
            header[i]: (row[i] if i < len(row) else "")
            for i in range(len(header))
        }
        result.append(row_dict)
    return result


# -----------------------------------------------------------------------
# MAIN EXTRACTION
# -----------------------------------------------------------------------

def extract_pdf_structure(pdf_path: str) -> list:
    """
    Parse a PDF and return a list of section dicts:

        [
            {
                "header": "1. Introduction",
                "content": [
                    {"type": "text", "data": "..."},
                    {"type": "structured_table", "data": [...]},
                    {"type": "unstructured_table", "data": [...]},
                ]
            },
            ...
        ]

    Content before the first detected header is silently dropped.
    """
    document: list = []
    current_section: dict | None = None

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            logger.info("Processing page %d / %d", page_num, len(pdf.pages))

            chars = page.chars
            lines = group_chars_to_lines(chars)
            words = page.extract_words()

            # Collect tables with their bounding boxes
            structured_tables = [
                (t.extract(), t.bbox) for t in page.find_tables()
            ]

            # Remove any unstructured (gap-detected) table whose vertical span
            # overlaps a structured table — prevents double-extraction when a
            # bordered table also happens to have large column gaps.
            structured_bboxes = [
                {"top": bbox[1], "bottom": bbox[3]}
                for _, bbox in structured_tables
            ]

            def overlaps_structured(utbl_bbox: dict) -> bool:
                return any(
                    is_inside_bbox(utbl_bbox["top"], utbl_bbox["bottom"], sb)
                    for sb in structured_bboxes
                )

            unstructured_tables = [
                (rows, bbox)
                for rows, bbox in extract_unstructured_tables_with_bbox(words)
                if not overlaps_structured(bbox)
            ]

            used_bboxes: list = []
            text_buffer: list = []

            for line in lines:
                text = line_to_text(line)
                if not text.strip():
                    continue

                line_top = min(c["top"] for c in line)
                line_bottom = max(c["bottom"] for c in line)

                # Skip content already captured inside a table bbox
                if any(is_inside_bbox(line_top, line_bottom, b) for b in used_bboxes):
                    continue

                # ---- HEADER ----
                if is_header_line(line):
                    flush_text_buffer(text_buffer, current_section)
                    current_section = {"header": text, "content": []}
                    document.append(current_section)
                    continue

                # Drop content that precedes any section header
                if current_section is None:
                    continue

                handled = False

                # ---- STRUCTURED TABLE ----
                for raw_table, bbox in structured_tables:
                    table_bbox = {"top": bbox[1], "bottom": bbox[3]}
                    if is_inside_bbox(line_top, line_bottom, table_bbox):
                        flush_text_buffer(text_buffer, current_section)
                        current_section["content"].append({
                            "type": "structured_table",
                            "data": build_structured_table(raw_table),
                        })
                        used_bboxes.append(table_bbox)
                        handled = True
                        break

                # ---- UNSTRUCTURED TABLE ----
                if not handled:
                    for table_rows, bbox in unstructured_tables:
                        if is_inside_bbox(line_top, line_bottom, bbox):
                            flush_text_buffer(text_buffer, current_section)
                            current_section["content"].append({
                                "type": "unstructured_table",
                                "data": table_rows,
                            })
                            used_bboxes.append(bbox)
                            handled = True
                            break

                # ---- PLAIN TEXT ----
                if not handled:
                    text_buffer.append(text)

            # Flush any remaining text at end of page
            flush_text_buffer(text_buffer, current_section)

    return document


# -----------------------------------------------------------------------
# OUTPUT
# -----------------------------------------------------------------------

def save_json(data: list, output_path: str) -> None:
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    logger.info("Saved output to %s", output_path)


# -----------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract structured content (headers, text, tables) from a PDF."
    )
    parser.add_argument("pdf_path", help="Path to the input PDF file.")
    parser.add_argument(
        "output_path",
        nargs="?",
        help="Path for the JSON output (default: same name as PDF with .json extension).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        logger.error("PDF not found: %s", pdf_path)
        sys.exit(1)

    output_path = args.output_path or pdf_path.with_suffix(".json")

    data = extract_pdf_structure(str(pdf_path))
    save_json(data, str(output_path))
    print(f"Done — {len(data)} section(s) extracted → {output_path}")


if __name__ == "__main__":
    main()