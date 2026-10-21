---
name: pdf-batch-processing-tool
description: Batch process PDF files - merge multiple PDFs, split PDF into multiple files, rotate pages, extract text, extract images, compress PDFs. Use when you need to process multiple PDF files in bulk.
---

# PDF Batch Processor

Batch process multiple PDF files with common operations. No need for expensive online services - process locally, keep your data private.

## Core Capabilities

### 1. **Merge multiple PDFs**
- Combine multiple PDF files into one
- Preserve page order
- Add table of contents optional

### 2. **Split a PDF**
- Split by page ranges
- Split each page into a separate file
- Extract specific pages

### 3. **Rotate pages**
- Rotate all pages or specific page ranges
- Support 90/180/270 degree rotation

### 4. **Extract text**
- Extract text from all pages
- Export to plain text or markdown
- Batch extract from multiple PDFs in a folder

### 5. **Extract images**
- Save all images from a PDF to separate image files
- Preserve original image quality when possible

### 6. **Compress PDF**
- Reduce file size for web/email
- Three compression levels (low/medium/high)

## Usage Examples

### Merge multiple PDFs
```bash
python scripts/merge_pdfs.py --output combined.pdf file1.pdf file2.pdf file3.pdf
```

### Split PDF into individual pages
```bash
python scripts/split_pdfs.py --input document.pdf --output output-folder/ --mode pages
```

### Extract all text from PDFs in a folder
```bash
python scripts/extract_text.py --input ./pdfs/ --output ./text/
```

### Rotate all pages 90 degrees clockwise
```bash
python scripts/rotate_pdf.py --input input.pdf --output output.pdf --degrees 90
```

## Installation

```bash
pip install pypdf pillow
```

## When to use this skill

✅ **Use when:**
- You have multiple PDFs that need the same operation
- You want to keep processing local (private, no uploads needed)
- You need to automate PDF processing in a workflow

❌ **Don't use when:**
- You only need to edit one page manually (use a GUI PDF editor)
- The PDF is encrypted/scanned image-only (needs OCR first)
- You need advanced editing (add/remove content, edit text)

## Notes

- Works with standard PDF files
- For scanned/image PDFs you need OCR first (use an OCR tool before processing)
- All processing is local - your files never leave your machine
