#!/usr/bin/env python3
"""
Merge multiple PDF files into a single PDF.
"""

import argparse
import os
from pypdf import PdfWriter, PdfReader

def merge_pdfs(pdf_paths: list, output_path: str, add_bookmarks: bool = False) -> None:
    """
    Merge multiple PDF files into one.
    
    Args:
        pdf_paths: List of paths to PDF files in order
        output_path: Path for the merged output PDF
        add_bookmarks: Add bookmarks for each input PDF
    """
    writer = PdfWriter()
    
    current_page = 0
    
    for pdf_path in pdf_paths:
        if not os.path.exists(pdf_path):
            print(f"Warning: {pdf_path} not found, skipping...")
            continue
        
        reader = PdfReader(pdf_path)
        num_pages = len(reader.pages)
        
        # Add all pages
        writer.append(reader)
        
        # Add bookmark if requested
        if add_bookmarks:
            filename = os.path.basename(pdf_path)
            writer.add_bookmark(filename, current_page)
        
        current_page += num_pages
        print(f"Added {pdf_path} ({num_pages} pages)")
    
    with open(output_path, "wb") as output_file:
        writer.write(output_file)
    
    print(f"\nDone! Merged {current_page} pages -> {output_path}")

def main():
    parser = argparse.ArgumentParser(description='Merge multiple PDF files into one.')
    parser.add_argument('pdfs', nargs='+', help='PDF files to merge in order')
    parser.add_argument('--output', required=True, help='Output PDF file path')
    parser.add_argument('--bookmarks', action='store_true', help='Add bookmarks for each input file')
    
    args = parser.parse_args()
    merge_pdfs(args.pdfs, args.output, args.bookmarks)

if __name__ == "__main__":
    main()
