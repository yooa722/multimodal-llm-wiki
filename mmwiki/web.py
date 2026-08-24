from __future__ import annotations

import html
import posixpath
import re
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote


DEFAULT_DEMO_BASE_URL = "http://127.0.0.1:19828"


def wiki_view_url(relative_path: str, base_url: str = DEFAULT_DEMO_BASE_URL) -> str:
    encoded = quote(relative_path, safe="/")
    return f"{base_url.rstrip('/')}/wiki/view?path={encoded}"


def media_url(relative_path: str, base_url: str = DEFAULT_DEMO_BASE_URL) -> str:
    encoded = quote(relative_path, safe="/")
    return f"{base_url.rstrip('/')}/api/v1/media/{encoded}"


def query_view_url(
    query_id: str,
    evidence: int = 1,
    view: str = "wiki",
    base_url: str = DEFAULT_DEMO_BASE_URL,
) -> str:
    return (
        f"{base_url.rstrip('/')}/query/view?id={quote(str(query_id), safe='-_.')}"
        f"&evidence={max(1, int(evidence))}&view={'evidence' if view == 'evidence' else 'wiki'}"
    )


def _query_evidence_entries(record: dict[str, Any]) -> list[dict[str, Any]]:
    citations = record.get("citations", [])
    evidence_ids = [str(value) for value in record.get("evidence_refs", [])]
    if not evidence_ids:
        evidence_ids = [
            str(evidence_id)
            for citation in citations
            for evidence_id in citation.get("evidence_ids", [])
        ]
    entries = []
    for evidence_id in dict.fromkeys(value for value in evidence_ids if value):
        citation = next(
            (
                value
                for value in citations
                if evidence_id in set(map(str, value.get("evidence_ids", [])))
            ),
            {},
        )
        item_id = evidence_id.partition("#")[2].partition("#")[0]
        entries.append(
            {
                "evidence_id": evidence_id,
                "item_id": item_id,
                "citation": citation,
            }
        )
    return entries


def _query_answer_html(
    answer: str, query_id: str, entries: list[dict[str, Any]], base_url: str
) -> str:
    rendered = html.escape(str(answer or "未生成回答"))
    cited: set[int] = set()
    for index, entry in enumerate(entries, start=1):
        evidence_id = html.escape(str(entry["evidence_id"]))
        link = html.escape(query_view_url(query_id, index, "wiki", base_url), quote=True)
        marker = f'<a class="cite" href="{link}">〔{index}〕</a>'
        for variant in (
            f"〔{evidence_id}〕",
            f"【{evidence_id}】",
            f"`{evidence_id}`",
            evidence_id,
        ):
            if variant in rendered:
                rendered = rendered.replace(variant, marker)
                cited.add(index)
    rendered = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"`([^`]+)`", r"<code>\1</code>", rendered)
    paragraphs = [value.strip() for value in rendered.split("\n\n") if value.strip()]
    body = "".join(f"<p>{value.replace(chr(10), '<br>')}</p>" for value in paragraphs)
    missing = [index for index in range(1, len(entries) + 1) if index not in cited]
    if missing:
        links = " ".join(
            f'<a class="cite" href="{html.escape(query_view_url(query_id, index, "wiki", base_url), quote=True)}">〔{index}〕</a>'
            for index in missing
        )
        body += f'<p class="answer-refs"><strong>引用：</strong>{links}</p>'
    return body


def render_query_html(
    record: dict[str, Any], base_url: str, evidence: int = 1, view: str = "wiki"
) -> bytes:
    """Render a split query workspace with the answer left and Wiki right."""

    query_id = str(record.get("query_id") or "")
    entries = _query_evidence_entries(record)
    selected_index = min(max(1, int(evidence)), max(1, len(entries)))
    selected = entries[selected_index - 1] if entries else {}
    citation = selected.get("citation") or {}
    item_id = str(selected.get("item_id") or "")
    raw_path = str(citation.get("path") or "")
    raw_url = wiki_view_url(raw_path, base_url)
    if item_id:
        raw_url += "#" + quote(item_id, safe="-_.:")

    retrieval = record.get("retrieval") or {}
    navigation = retrieval.get("wiki_navigation") or []
    wiki_paths = set(map(str, citation.get("wiki_paths", [])))
    selected_page = next(
        (page for page in navigation if str(page.get("path") or "") in wiki_paths),
        None,
    )
    if selected_page is None:
        selected_page = next(
            (
                {"path": path, "title": Path(path).stem}
                for path in citation.get("wiki_paths", [])
                if "/evidence/" not in str(path) and "/sources/" not in str(path)
            ),
            None,
        )
    wiki_path = str((selected_page or {}).get("path") or raw_path)
    wiki_url = wiki_view_url(wiki_path, base_url)
    active_view = "evidence" if view == "evidence" else "wiki"
    panel_url = raw_url if active_view == "evidence" else wiki_url
    panel_title = (
        "原始 Evidence"
        if active_view == "evidence"
        else str((selected_page or {}).get("title") or "Wiki 页面")
    )

    evidence_tabs = "".join(
        f'<a class="evidence-tab{" active" if index == selected_index else ""}" '
        f'href="{html.escape(query_view_url(query_id, index, active_view, base_url), quote=True)}">'
        f"Evidence {index}</a>"
        for index in range(1, len(entries) + 1)
    ) or '<span class="empty">没有可展示的 Evidence</span>'
    wiki_switch = query_view_url(query_id, selected_index, "wiki", base_url)
    evidence_switch = query_view_url(query_id, selected_index, "evidence", base_url)
    answer_html = _query_answer_html(
        str(record.get("answer") or ""), query_id, entries, base_url
    )
    page = str((citation.get("pages") or ["未记录"])[0])
    modality = "、".join(map(str, citation.get("modalities", []))) or "未记录"
    evidence_id = str(selected.get("evidence_id") or "未记录")
    question = html.escape(str(record.get("question") or ""))
    safe_panel_url = html.escape(panel_url, quote=True)
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>问答核验 · 多模态 LLM Wiki</title>
<style>
:root{{--blue:#1769d2;--navy:#12345a;--ink:#152238;--muted:#607086;--line:#dbe5f1;--paper:#f4f7fb}}
*{{box-sizing:border-box}}html,body{{height:100%}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.shell{{display:grid;grid-template-columns:minmax(360px,42%) minmax(520px,58%);height:100vh}}
.answer-pane{{padding:30px 34px;overflow:auto;background:#fff;border-right:1px solid var(--line)}}
.wiki-pane{{display:flex;min-width:0;flex-direction:column;overflow:hidden}}
.eyebrow{{color:var(--blue);font-size:13px;font-weight:750;letter-spacing:.08em}}h1{{font-size:25px;line-height:1.3;margin:8px 0 22px}}h2{{font-size:17px;margin:28px 0 10px}}
.question{{padding:14px 16px;background:#f2f7ff;border-left:4px solid var(--blue);border-radius:8px}}.answer{{font-size:17px}}
.cite{{display:inline-flex;align-items:center;justify-content:center;color:var(--blue);font-weight:750;text-decoration:none;margin:0 2px}}code{{background:#edf3fb;padding:2px 5px;border-radius:5px}}
.run{{display:grid;grid-template-columns:auto 1fr;gap:7px 14px;color:var(--muted);font-size:13px}}.run strong{{color:var(--ink)}}
.panel-head{{padding:16px 20px;background:#fff;border-bottom:1px solid var(--line)}}.panel-row{{display:flex;align-items:center;justify-content:space-between;gap:16px}}.panel-title{{font-size:17px;font-weight:750}}
.tabs,.switch{{display:flex;gap:7px;flex-wrap:wrap}}.evidence-tab,.switch a{{padding:5px 10px;border:1px solid var(--line);border-radius:999px;color:var(--muted);text-decoration:none;background:#fff}}.evidence-tab.active,.switch a.active{{color:#fff;background:var(--blue);border-color:var(--blue)}}
.meta{{margin-top:10px;color:var(--muted);font-size:13px;word-break:break-all}}iframe{{width:100%;height:100%;border:0;background:#fff}}.empty{{color:var(--muted)}}
@media(max-width:900px){{.shell{{display:block;height:auto}}.answer-pane{{border-right:0;border-bottom:1px solid var(--line)}}.wiki-pane{{height:72vh}}}}
</style>
</head>
<body><div class="shell">
<section class="answer-pane">
<div class="eyebrow">MULTIMODAL LLM WIKI</div>
<h1>问答与证据核验</h1>
<div class="question"><strong>问题</strong><br>{question}</div>
<h2>结论</h2><div class="answer">{answer_html}</div>
<h2>运行信息</h2>
<div class="run"><strong>检索模式</strong><span>请求 {html.escape(str(retrieval.get('requested_mode') or '未记录'))} / 实际 {html.escape(str(retrieval.get('mode') or '未记录'))}</span><strong>路由依据</strong><span>{html.escape(str(retrieval.get('routing_reason') or '未记录'))}</span><strong>回退原因</strong><span>{html.escape(str(retrieval.get('fallback_reason') or '无'))}</span><strong>回答模型</strong><span>{html.escape(str(record.get('model') or '未记录'))}</span><strong>Query ID</strong><span>{html.escape(query_id)}</span></div>
</section>
<section class="wiki-pane" id="evidence-panel">
<div class="panel-head"><div class="panel-row"><div class="panel-title">{html.escape(panel_title)}</div><div class="switch"><a class="{'active' if active_view == 'wiki' else ''}" href="{html.escape(wiki_switch, quote=True)}">Wiki 页面</a><a class="{'active' if active_view == 'evidence' else ''}" href="{html.escape(evidence_switch, quote=True)}">原始 Evidence</a></div></div><div class="tabs">{evidence_tabs}</div><div class="meta">Evidence ID：{html.escape(evidence_id)}　·　第 {html.escape(page)} 页　·　{html.escape(modality)}</div></div>
<iframe title="{html.escape(panel_title, quote=True)}" src="{safe_panel_url}"></iframe>
</section>
</div></body></html>"""
    return document.encode("utf-8")


def resolve_vault_path(
    vault_root: Path,
    raw_path: str,
    *,
    required_prefix: str | tuple[str, ...],
    allowed_suffixes: set[str],
) -> Path:
    decoded = unquote(raw_path)
    if not decoded or "\x00" in decoded:
        raise ValueError("资源路径不能为空")
    logical = PurePosixPath(decoded)
    if logical.is_absolute() or ".." in logical.parts:
        raise ValueError("资源路径不能逃逸 Vault")
    required_prefixes = (
        (required_prefix,) if isinstance(required_prefix, str) else required_prefix
    )
    if not logical.parts or logical.parts[0] not in required_prefixes:
        raise ValueError(f"资源路径必须位于 {' 或 '.join(required_prefixes)}/")
    candidate = (vault_root / Path(*logical.parts)).resolve()
    root = vault_root.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("资源路径不能逃逸 Vault")
    if candidate.suffix.lower() not in allowed_suffixes:
        raise ValueError("不支持的资源类型")
    if not candidate.is_file():
        raise FileNotFoundError(decoded)
    return candidate


def _inline(value: str, base_url: str, image_base: str = "") -> str:
    tokens: list[str] = []

    def token(fragment: str) -> str:
        index = len(tokens)
        tokens.append(fragment)
        return f"\x00TOKEN{index}\x00"

    def obsidian_image(match: re.Match[str]) -> str:
        path = match.group(1).split("|", 1)[0].strip()
        url = media_url(path, base_url)
        return token(
            f'<figure><a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">'
            f'<img src="{html.escape(url, quote=True)}" alt="原始视觉 Evidence"></a>'
            f'<figcaption><a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">'
            "在浏览器中打开原图</a></figcaption></figure>"
        )

    def standard_image(match: re.Match[str]) -> str:
        alt = match.group(1).strip()
        path = match.group(2).strip()
        title = re.match(r"^(.*?)(?:\s+[\"'].*[\"'])$", path)
        path = title.group(1) if title else path
        if (
            image_base
            and not path.startswith("assets/")
            and not PurePosixPath(path).is_absolute()
            and not re.match(
            r"^[A-Za-z][A-Za-z0-9+.-]*:", path
            )
        ):
            path = posixpath.normpath(
                (PurePosixPath(image_base).parent / PurePosixPath(path)).as_posix()
            )
        url = media_url(path, base_url)
        escaped_url = html.escape(url, quote=True)
        escaped_alt = html.escape(alt, quote=True)
        return token(
            f'<figure><a href="{escaped_url}" target="_blank" rel="noopener">'
            f'<img src="{escaped_url}" alt="{escaped_alt}"></a>'
            f'<figcaption>{escaped_alt}</figcaption></figure>'
        )

    def wiki_link(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        label = (match.group(2) or target).strip()
        target_path, separator, anchor = target.partition("#")
        if not target_path.endswith(".md"):
            target_path += ".md"
        url = wiki_view_url(target_path, base_url)
        if separator and anchor:
            url += "#" + quote(anchor, safe="-_")
        return token(
            f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">'
            f"{html.escape(label)}</a>"
        )

    value = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", standard_image, value)
    value = re.sub(r"!\[\[([^\]]+)\]\]", obsidian_image, value)
    value = re.sub(r"\[\[([^|\]]+)(?:\|([^\]]+))?\]\]", wiki_link, value)
    escaped = html.escape(value)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    for index, fragment in enumerate(tokens):
        escaped = escaped.replace(html.escape(f"\x00TOKEN{index}\x00"), fragment)
    return escaped


def _render_table(lines: list[str], base_url: str, image_base: str = "") -> str:
    rows: list[list[str]] = []
    for line in lines:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append(cells)
    if len(rows) > 1 and all(re.fullmatch(r":?-{3,}:?", cell) for cell in rows[1]):
        rows.pop(1)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = "".join(f"<th>{_inline(cell, base_url, image_base)}</th>" for cell in rows[0])
    body = "".join(
        "<tr>" + "".join(f"<td>{_inline(cell, base_url, image_base)}</td>" for cell in row) + "</tr>"
        for row in rows[1:]
    )
    return f"<div class=table-wrap><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>"


def render_wiki_html(markdown: str, relative_path: str, base_url: str) -> bytes:
    lines = markdown.splitlines()
    metadata: list[str] = []
    if lines and lines[0].strip() == "---":
        try:
            closing = lines.index("---", 1)
        except ValueError:
            closing = -1
        if closing > 0:
            metadata = lines[1:closing]
            lines = lines[closing + 1 :]

    title = Path(relative_path).stem
    for line in metadata:
        if line.startswith("title:"):
            title = line.split(":", 1)[1].strip().strip('"') or title
            break

    blocks: list[str] = []
    index = 0
    in_code = False
    code_lines: list[str] = []
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                blocks.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
                code_lines = []
                in_code = False
            else:
                in_code = True
            index += 1
            continue
        if in_code:
            code_lines.append(line)
            index += 1
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            table_lines = []
            while index < len(lines):
                candidate = lines[index].strip()
                if not (candidate.startswith("|") and candidate.endswith("|")):
                    break
                table_lines.append(candidate)
                index += 1
            blocks.append(_render_table(table_lines, base_url, relative_path))
            continue
        anchor = re.fullmatch(r'<a\s+id="([A-Za-z0-9_.:-]+)"></a>', stripped)
        if anchor:
            blocks.append(f'<span id="{html.escape(anchor.group(1), quote=True)}"></span>')
        elif match := re.match(r"^(#{1,6})\s+(.+)$", stripped):
            level = len(match.group(1))
            blocks.append(f"<h{level}>{_inline(match.group(2), base_url, relative_path)}</h{level}>")
        elif match := re.match(r"^-\s+(.+)$", stripped):
            items = [match.group(1)]
            index += 1
            while index < len(lines):
                next_match = re.match(r"^-\s+(.+)$", lines[index].strip())
                if not next_match:
                    break
                items.append(next_match.group(1))
                index += 1
            blocks.append("<ul>" + "".join(f"<li>{_inline(item, base_url, relative_path)}</li>" for item in items) + "</ul>")
            continue
        elif match := re.match(r"^\d+\.\s+(.+)$", stripped):
            items = [match.group(1)]
            index += 1
            while index < len(lines):
                next_match = re.match(r"^\d+\.\s+(.+)$", lines[index].strip())
                if not next_match:
                    break
                items.append(next_match.group(1))
                index += 1
            blocks.append("<ol>" + "".join(f"<li>{_inline(item, base_url, relative_path)}</li>" for item in items) + "</ol>")
            continue
        elif stripped.startswith(">"):
            blocks.append(f"<blockquote>{_inline(stripped.lstrip('>').strip(), base_url, relative_path)}</blockquote>")
        elif stripped.startswith(("![[", "![")):
            blocks.append(_inline(stripped, base_url, relative_path))
        elif stripped:
            blocks.append(f"<p>{_inline(stripped, base_url, relative_path)}</p>")
        index += 1

    raw_url = f"{base_url.rstrip('/')}/api/v1/wiki/raw?path={quote(relative_path, safe='/')}"
    body = "\n".join(blocks)
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · 多模态 LLM Wiki</title>
<style>
:root{{--blue:#1d63c6;--ink:#142033;--muted:#5c6b7f;--line:#dbe5f2;--paper:#f5f8fc}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.72 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:980px;margin:32px auto;padding:38px 46px;background:#fff;border:1px solid var(--line);border-radius:18px;box-shadow:0 10px 32px rgba(31,78,121,.08)}}
.top{{display:flex;justify-content:space-between;gap:20px;align-items:center;border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:26px}}
.eyebrow{{color:var(--blue);font-weight:700}} .path{{color:var(--muted);font-size:13px;word-break:break-all}}
h1,h2,h3{{line-height:1.3;margin:1.25em 0 .55em}} h1{{font-size:32px}} h2{{font-size:24px;border-bottom:1px solid var(--line);padding-bottom:8px}} h3{{font-size:19px}}
a{{color:var(--blue);text-decoration:none}} a:hover{{text-decoration:underline}} code{{background:#edf3fb;padding:2px 6px;border-radius:5px;word-break:break-all}}
blockquote{{margin:18px 0;padding:12px 18px;border-left:4px solid var(--blue);background:#f2f7ff;color:#31445e}}
.table-wrap{{overflow:auto;margin:18px 0}} table{{border-collapse:collapse;width:100%;font-size:14px}} th,td{{border:1px solid var(--line);padding:10px 12px;text-align:left}} th{{background:#edf4ff}}
figure{{margin:22px 0}} img{{display:block;max-width:100%;max-height:760px;border:1px solid var(--line);border-radius:10px;background:white}} figcaption{{margin-top:8px;font-size:14px}}
pre{{overflow:auto;background:#101827;color:#eaf2ff;padding:16px;border-radius:10px}} .raw{{white-space:nowrap}}
@media(max-width:700px){{main{{margin:0;padding:24px 18px;border-radius:0}}.top{{display:block}}}}
</style>
</head>
<body><main>
<div class="top"><div><div class="eyebrow">多模态 LLM Wiki</div><div class="path">{html.escape(relative_path)}</div></div><a class="raw" href="{html.escape(raw_url, quote=True)}" target="_blank">查看原始 Markdown</a></div>
{body}
</main></body></html>"""
    return document.encode("utf-8")
