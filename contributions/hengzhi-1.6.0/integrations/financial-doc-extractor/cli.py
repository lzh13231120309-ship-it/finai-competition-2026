#!/usr/bin/env python3
"""Batch extraction with source text, evidence, parameters and version records."""
import argparse
import json
import sys
from pathlib import Path
from audit import write_audit,environment
from engine import extract_document,resolve_schemas
from pdf_reader import read_pdf


def main():
    parser=argparse.ArgumentParser(description='金融 PDF 批量结构化提取；每个文件生成可复核的 JSON 报告')
    parser.add_argument('pdf',nargs='+')
    parser.add_argument('--schema',default=None)
    parser.add_argument('--pages',default=None)
    parser.add_argument('--ocr',choices=['auto','off','force'],default='auto')
    parser.add_argument('--ocr-backend',choices=['auto','vision','tesseract'],default='auto')
    parser.add_argument('--header-rows',type=int,default=1)
    parser.add_argument('--output-dir',default='runs')
    args=parser.parse_args()
    try:resolve_schemas(args.schema)
    except ValueError as exc:parser.error(str(exc))
    failed=False
    for file in args.pdf:
        parameters={'pdf_path':file,'schema':args.schema,'page_range':args.pages,'ocr_mode':args.ocr,'ocr_backend':args.ocr_backend,'header_rows':args.header_rows}
        try:
            parsed=read_pdf(file,page_range=args.pages,ocr_mode=args.ocr,ocr_backend=args.ocr_backend,header_rows=args.header_rows)
            result=extract_document(parsed,args.schema)
            report=write_audit(args.output_dir,'batch_extract',parameters,result,document=parsed)
            print(json.dumps({'file':file,'report':str(report.resolve()),'status':parsed['status']},ensure_ascii=False))
            failed=failed or parsed['status']!='ok'
        except Exception as exc:
            failed=True
            report=write_audit(args.output_dir,'batch_extract',parameters,{'error':str(exc)})
            print(json.dumps({'file':file,'error':str(exc),'report':str(report.resolve())},ensure_ascii=False),file=sys.stderr)
    return 1 if failed else 0


if __name__=='__main__':raise SystemExit(main())
