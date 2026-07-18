#!/usr/bin/env python3
"""
Split a PDF into multiple files.
"""

import argparse
import os
from pypdf import PdfReader, PdfWriter

def split_by_pages(input_path: str, output_dir: str) -> None:
    """Split PDF into individual pages, one per file."""
    reader = PdfReader(input_path)
    os.makedirs(output_dir, exist_ok=True)
    
    basename = os.path.splitext(os.path.basename(input_path))[0]
    
    for i, page in enumerate(reader.pages, 1):
        writer = PdfWriter()
        writer.add_page(page)
        
        output_path = os.path.join(output_dir, f"{basename}_page_{i:03d}.pdf")
        with open(output_path, "wb") as f:
            writer.write(f)
        
        print(f"Wrote {output_path}")
    
    print(f"\nDone! Split {len(reader.pages)} pages into {output_dir}")

def split_by_ranges(input_path: str, output_dir: str, ranges: list) -> None:
    """Split PDF by page ranges."""
    reader = PdfReader(input_path)
    os.makedirs(output_dir, exist_ok=True)
    
    total_pages = len(reader.pages)
    
    for i, (start, end) in enumerate(ranges, 1):
        # Pages are 1-indexed in user input
        start_idx = max(0, start - 1)
        end_idx = min(total_pages, end)
        
        writer = PdfWriter()
        for page_idx in range(start_idx, end_idx):
            writer.add_page(reader.pages[page_idx])
        
        output_path = os.path.join(output_dir, f"part_{i:02d}_pages_{start}-{end}.pdf")
        with open(output_path, "wb") as f:
            writer.write(f)
        
        print(f"Wrote {output_path} ({end - start} pages)")
    
    print(f"\nDone! Split into {len(ranges)} parts")

def extract_pages(input_path: str, output_path: str, pages: list) -> None:
    """Extract specific pages into a new PDF."""
    reader = PdfReader(input_path)
    writer = PdfWriter()
    
    for page_num in pages:
        # Page numbers 1-indexed
        if 1 <= page_num <= len(reader.pages):
            writer.add_page(reader.pages[page_num - 1])
    
    with open(output_path, "wb") as f:
        writer.write(f)
    
    print(f"Extracted {len(pages)} pages -> {output_path}")

def parse_ranges(ranges_str: str) -> list:
    """Parse page ranges from string like '1-10,15-20'."""
    ranges = []
    parts = ranges_str.replace(' ', '').split(',')
    for part in parts:
        if '-' in part:
            start, end = part.split('-')
            ranges.append((int(start), int(end)))
        else:
            # Single page becomes its own range
            num = int(part)
            ranges.append((num, num))
    return ranges

def main():
    parser = argparse.ArgumentParser(description='Split a PDF file.')
    parser.add_argument('--input', required=True, help='Input PDF file')
    parser.add_argument('--output-dir', required=True, help='Output directory')
    parser.add_argument('--mode', choices=['pages', 'ranges'], default='pages', 
                        help='Split mode: one file per page or split by ranges')
    parser.add_argument('--ranges', help='Page ranges (e.g., "1-10,12-15")')
    
    args = parser.parse_args()
    
    if args.mode == 'pages':
        split_by_pages(args.input, args.output_dir)
    elif args.mode == 'ranges':
        if not args.ranges:
            print("Error: --ranges required for 'ranges' mode", file=sys.stderr)
            exit(1)
        ranges = parse_ranges(args.ranges)
        split_by_ranges(args.input, args.output_dir, ranges)

if __name__ == "__main__":
    import sys
    main()
