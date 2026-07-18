#!/usr/bin/env python3
"""
Rotate pages in a PDF file.
"""

import argparse
from pypdf import PdfReader, PdfWriter

def rotate_pdf(input_path: str, output_path: str, degrees: int, pages: list = None) -> None:
    """
    Rotate pages in a PDF.
    
    Args:
        input_path: Input PDF file
        output_path: Output PDF file
        degrees: Rotation in degrees (90, 180, 270 clockwise)
        pages: List of specific pages to rotate (1-indexed). If None, rotate all pages.
    """
    reader = PdfReader(input_path)
    writer = PdfWriter()
    
    total_pages = len(reader.pages)
    
    for i, page in enumerate(reader.pages, 1):
        if pages is None or i in pages:
            page.rotate(degrees)
        writer.add_page(page)
    
    with open(output_path, "wb") as f:
        writer.write(f)
    
    if pages:
        print(f"Rotated {len(pages)} pages {degrees} degrees -> {output_path}")
    else:
        print(f"Rotated all {total_pages} pages {degrees} degrees -> {output_path}")

def parse_page_list(pages_str: str) -> list:
    """Parse page list from string like '1,3,5-7'."""
    pages = []
    parts = pages_str.replace(' ', '').split(',')
    for part in parts:
        if '-' in part:
            start, end = map(int, part.split('-'))
            pages.extend(range(start, end + 1))
        else:
            pages.append(int(part))
    return pages

def main():
    parser = argparse.ArgumentParser(description='Rotate pages in a PDF.')
    parser.add_argument('--input', required=True, help='Input PDF file')
    parser.add_argument('--output', required=True, help='Output PDF file')
    parser.add_argument('--degrees', type=int, choices=[90, 180, 270, -90], 
                        required=True, help='Rotation degrees clockwise')
    parser.add_argument('--pages', help='Specific pages to rotate (e.g., "1,3,5-7")')
    
    args = parser.parse_args()
    
    if args.pages:
        pages = parse_page_list(args.pages)
    else:
        pages = None
    
    rotate_pdf(args.input, args.output, args.degrees, pages)

if __name__ == "__main__":
    main()
