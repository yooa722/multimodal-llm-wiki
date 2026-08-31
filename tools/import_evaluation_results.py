#!/usr/bin/env python3
"""Convert the official image-text QA result workbook into pending JSONL cases.

The workbook is an offline evaluation asset.  Its standard answers and legacy
RAG answers must never be ingested into the Wiki or exposed to the query path.
Source, page, and Evidence labels are intentionally left pending until the
corresponding source documents have been parsed and indexed.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import statistics
import unicodedata
import zipfile
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
EXPECTED_HEADERS = [
    "数据集ID",
    "会话ID",
    "问题ID",
    "问题",
    "标准回答",
    "RAG生成答案",
]


def _column_index(cell_reference: str) -> int:
    match = re.match(r"([A-Z]+)", cell_reference.upper())
    if not match:
        raise ValueError(f"无效的 Excel 单元格坐标: {cell_reference}")
    index = 0
    for char in match.group(1):
        index = index * 26 + ord(char) - ord("A") + 1
    return index - 1


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    namespace = {"m": MAIN_NS}
    return [
        "".join(node.text or "" for node in item.findall(".//m:t", namespace))
        for item in root.findall("m:si", namespace)
    ]


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    namespace = {"m": MAIN_NS, "r": REL_NS}
    first_sheet = workbook.find("m:sheets/m:sheet", namespace)
    if first_sheet is None:
        raise ValueError("Excel 中没有工作表")
    relation_id = first_sheet.attrib.get(f"{{{REL_NS}}}id")
    if not relation_id:
        raise ValueError("无法解析第一个工作表的关系 ID")

    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    target = None
    for relation in relationships.findall(f"{{{PKG_REL_NS}}}Relationship"):
        if relation.attrib.get("Id") == relation_id:
            target = relation.attrib.get("Target")
            break
    if not target:
        raise ValueError("无法解析第一个工作表的文件路径")

    if target.startswith("/"):
        sheet_path = target.lstrip("/")
    else:
        sheet_path = posixpath.normpath(posixpath.join("xl", target))
    if sheet_path.startswith("../") or sheet_path not in archive.namelist():
        raise ValueError(f"工作表路径无效: {sheet_path}")
    return sheet_path


def read_first_sheet(path: Path) -> list[list[str]]:
    """Read cell values from the first worksheet without a local Excel runtime."""
    with zipfile.ZipFile(path) as archive:
        shared_strings = _read_shared_strings(archive)
        sheet = ET.fromstring(archive.read(_first_sheet_path(archive)))

    namespace = {"m": MAIN_NS}
    rows: list[list[str]] = []
    for row in sheet.findall(".//m:sheetData/m:row", namespace):
        values: dict[int, str] = {}
        for cell in row.findall("m:c", namespace):
            reference = cell.attrib.get("r", "")
            column = _column_index(reference)
            cell_type = cell.attrib.get("t")
            if cell_type == "inlineStr":
                value = "".join(
                    node.text or "" for node in cell.findall(".//m:t", namespace)
                )
            else:
                value_node = cell.find("m:v", namespace)
                raw_value = value_node.text if value_node is not None else ""
                if cell_type == "s" and raw_value:
                    value = shared_strings[int(raw_value)]
                elif cell_type == "b":
                    value = "TRUE" if raw_value == "1" else "FALSE"
                else:
                    value = raw_value or ""
            values[column] = value.strip()
        if values:
            width = max(values) + 1
            rows.append([values.get(index, "") for index in range(width)])
    return rows


def _normalize_answer(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    value = re.sub(r"\s+", "", value)
    return re.sub(r"[，。；：、,.!?！？;:'\"“”‘’（）()\[\]【】]", "", value)


def rows_to_cases(rows: list[list[str]]) -> list[dict[str, object]]:
    if not rows:
        raise ValueError("Excel 工作表为空")
    headers = [str(value).strip() for value in rows[0][: len(EXPECTED_HEADERS)]]
    if headers != EXPECTED_HEADERS:
        raise ValueError(
            "Excel 表头不符合预期；应为: " + "、".join(EXPECTED_HEADERS)
        )

    cases: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for row_number, raw_row in enumerate(rows[1:], start=2):
        row = list(raw_row[: len(EXPECTED_HEADERS)])
        row.extend([""] * (len(EXPECTED_HEADERS) - len(row)))
        if not any(row):
            continue
        dataset_id, session_id, question_id, question, expected, legacy = row
        required = {
            "数据集ID": dataset_id,
            "会话ID": session_id,
            "问题ID": question_id,
            "问题": question,
            "标准回答": expected,
            "RAG生成答案": legacy,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"第 {row_number} 行缺少字段: {', '.join(missing)}")
        if question_id in seen_ids:
            raise ValueError(f"第 {row_number} 行问题 ID 重复: {question_id}")
        seen_ids.add(question_id)
        cases.append(
            {
                "id": question_id,
                "dataset_id": dataset_id,
                "session_id": session_id,
                "question": question,
                "expected_answer": expected,
                "legacy_rag_answer": legacy,
                "source_id": None,
                "modality": "pending",
                "difficulty": "pending",
                "evidence_item_ids": [],
                "page_refs": [],
                "wiki_page_paths": [],
                "expected_answerable": True,
                "annotation_status": "source_and_evidence_pending",
            }
        )
    if not cases:
        raise ValueError("Excel 中没有有效评测题")
    return cases


def baseline_summary(cases: Iterable[dict[str, object]]) -> dict[str, object]:
    rows = list(cases)
    expected_lengths = [len(str(row["expected_answer"])) for row in rows]
    legacy_lengths = [len(str(row["legacy_rag_answer"])) for row in rows]
    ratios = [
        legacy / max(expected, 1)
        for legacy, expected in zip(legacy_lengths, expected_lengths)
    ]
    exact = 0
    contains = 0
    evidence_ids = 0
    citation_markers = 0
    for row in rows:
        expected = _normalize_answer(str(row["expected_answer"]))
        legacy = _normalize_answer(str(row["legacy_rag_answer"]))
        exact += int(expected == legacy)
        contains += int(bool(expected) and expected in legacy)
        raw_legacy = str(row["legacy_rag_answer"])
        evidence_ids += int(bool(re.search(r"Evidence\s*ID", raw_legacy, re.I)))
        citation_markers += int(
            bool(re.search(r"(?:〔|\[|【)\s*\d+\s*(?:〕|\]|】)", raw_legacy))
        )
    return {
        "case_count": len(rows),
        "exact_normalized_match_count": exact,
        "gold_contained_in_legacy_count": contains,
        "legacy_with_evidence_id_count": evidence_ids,
        "legacy_with_citation_marker_count": citation_markers,
        "average_expected_answer_chars": round(statistics.mean(expected_lengths), 1),
        "average_legacy_answer_chars": round(statistics.mean(legacy_lengths), 1),
        "median_legacy_to_expected_length_ratio": round(statistics.median(ratios), 2),
        "legacy_over_3x_expected_count": sum(ratio > 3 for ratio in ratios),
    }


def write_jsonl(cases: Iterable[dict[str, object]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将图文回答结果 Excel 转为待补齐溯源标注的 JSONL 评测集"
    )
    parser.add_argument("workbook", type=Path, help="图文回答_Results.xlsx 路径")
    parser.add_argument("--output", type=Path, required=True, help="输出 JSONL 路径")
    parser.add_argument(
        "--summary-output", type=Path, help="可选：输出旧 RAG 基线统计 JSON"
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    cases = rows_to_cases(read_first_sheet(args.workbook))
    write_jsonl(cases, args.output)
    summary = baseline_summary(cases)
    if args.summary_output:
        args.summary_output.parent.mkdir(parents=True, exist_ok=True)
        args.summary_output.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"已写入: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
