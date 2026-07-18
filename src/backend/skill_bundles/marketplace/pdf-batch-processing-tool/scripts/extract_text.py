#!/usr/bin/env python3
"""
Extract text from one or more PDF files.
Supports single file or batch processing of all PDFs in a directory.
"""

import argparse
import os
from pypdf import PdfReader

def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract all text from a PDF file."""
    reader = PdfReader(pdf_path)
    text_parts = []
    
    for i, page in enumerate(reader.pages):
        page_text = page.extract_text()
        if page_text:
            text_parts.append(f"--- Page {i+1} ---\n")
            text_parts.append(page_text)
            text_parts.append("\n\n")
    
    return "".join(text_parts)

def batch_extract(input_dir: str, output_dir: str) -> None:
    """Extract text from all PDFs in a directory."""
    os.makedirs(output_dir, exist_ok=True)
    
    pdf_files = [f for f in os.listdir(input_dir) if f.lower().endswith('.pdf')]
    
    if not pdf_files:
        print(f"No PDF files found in {input_dir}")
        return
    
    for pdf_file in pdf_files:
        pdf_path = os.path.join(input_dir, pdf_file)
        basename = os.path.splitext(pdf_file)[0]
        output_path = os.path.join(output_dir, f"{basename}.txt")
        
        text = extract_text_from_pdf(pdf_path)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(text)
        
        print(f"Extracted: {pdf_file} -> {output_path} ({len(text)} characters)")
    
    print(f"\nDone! Processed {len(pdf_files)} PDF files")

def main():
    parser = argparse.ArgumentParser(description='Extract text from PDF files.')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--input', help='Single input PDF file')
    group.add_argument('--input-dir', help='Directory containing multiple PDF files')
    parser.add_argument('--output', help='Output text file (for single file mode)')
    parser.add_argument('--output-dir', help='Output directory (for batch mode)')
    
    args = parser.parse_args()
    
    if args.input:
        # Single file mode
        if not args.output:
            basename = os.path.splitext(args.input)[0]
            args.output = f"{basename}.txt"
        
        text = extract_text_from_pdf(args.input)
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(text)
        
        print(f"Extracted {len(text)} characters -> {args.output}")
    
    elif args.input_dir:
        # Batch mode
        if not args.output_dir:
            print("Error: --output-dir required for batch mode", file=sys.stderr)
            exit(1)
        
        batch_extract(args.input_dir, args.output_dir)

if __name__ == "__main__":
    import sys
    main()
