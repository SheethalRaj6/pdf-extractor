# PDF Extractor
(pdf to json format)
Extracts structured content from PDFs — section headers, plain text, 
and both bordered and key/value tables — using pdfplumber.

## Install
pip install pdfplumber

## Usage
python pdf_extractor.py input.pdf output.json

## Notes
- Headers detected by bold font + numbered pattern (e.g. "1.", "1.2", "Section 3")
- Tune `gap_threshold` in `has_large_gap()` for different PDF layouts
