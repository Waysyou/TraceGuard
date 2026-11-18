"""Render the limited Markdown emitted by our report, with no external assets."""
from html import escape, unescape
import re


def render_document(markdown: str) -> str:
    def inline(text):
        text = escape(unescape(text.replace(r"\|", "|")))
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)

    lines = markdown.splitlines()
    body = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                row = re.split(r"(?<!\\)\|", lines[i].strip())[1:-1]
                if not all(re.fullmatch(r"\s*:?-+:?\s*", cell) for cell in row):
                    rows.append(row)
                i += 1
            body.append('<div class="table-wrap"><table>')
            for number, row in enumerate(rows):
                tag = "th" if number == 0 else "td"
                body.append("<tr>" + "".join(f"<{tag}>{inline(cell.strip())}</{tag}>" for cell in row) + "</tr>")
            body.append("</table></div>")
            continue
        heading = re.match(r"^(#{1,3}) (.*)", line)
        if heading:
            level = len(heading[1])
            body.append(f"<h{level}>{inline(heading[2])}</h{level}>")
        elif line.startswith("- ") or re.match(r"^\d+\. ", line):
            ordered = not line.startswith("- ")
            tag = "ol" if ordered else "ul"
            body.append(f"<{tag}>")
            while i < len(lines) and (bool(re.match(r"^\d+\. ", lines[i])) if ordered else lines[i].startswith("- ")):
                text = re.sub(r"^\d+\. ", "", lines[i]) if ordered else lines[i][2:]
                body.append(f"<li>{inline(text)}</li>")
                i += 1
            body.append(f"</{tag}>")
            continue
        else:
            body.append(f"<p>{inline(line)}</p>")
        i += 1
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; connect-src 'none'">
<title>票迹 TraceGuard · 完整评测报告</title><style>
*{box-sizing:border-box}body{margin:32px auto;padding:0 24px;max-width:1180px;color:#24362f;background:#fff;font:15px/1.8 'Segoe UI','Microsoft YaHei',sans-serif}h1{font-size:27px}h2{font-size:21px;margin-top:34px;border-bottom:1px solid #d9e2dc;padding-bottom:8px}h3{font-size:17px;margin-top:24px}p,li,td{overflow-wrap:anywhere}code{font-size:12px;white-space:normal}.table-wrap{overflow-x:auto;margin:16px 0}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid #d9e2dc;padding:9px 10px;vertical-align:top;text-align:left}th{background:#edf4ef}ul,ol{padding-left:24px}@media(max-width:600px){body{margin:20px auto;padding:0 16px}table{min-width:700px}h1{font-size:23px}}@media print{body{max-width:none;margin:0;padding:0;font-size:10pt}h2,h3{break-after:avoid}tr{break-inside:avoid}.table-wrap{overflow:visible}table{min-width:0;font-size:8pt}thead{display:table-header-group}}
</style><main>''' + "\n".join(body) + "</main></html>"
