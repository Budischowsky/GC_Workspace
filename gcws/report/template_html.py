"""A Template report's table as HTML for the template window's live preview (Qt rich text)."""
from __future__ import annotations

from html import escape

from gcws.report.layout import RED, header_cells
from gcws.report.table import ReportTable, fmt


def to_html(table: ReportTable, max_rows: int = 300) -> str:
    cols = table.columns
    n = max(len(cols), 2)
    out = ['<html><body style="background:#ffffff; color:#000000; font-family:Arial; font-size:8pt;">',
           '<table width="100%" cellspacing="0" cellpadding="2" style="border-collapse:collapse;">',
           f'<tr><td colspan="{n}" bgcolor="#{RED}"><font color="#ffffff"><b>{escape(table.title)}</b></font></td></tr>',
           f'<tr><td colspan="{n}"><b><i>{escape(table.subtitle)}</i></b></td></tr>']
    for line in table.header_lines:
        out.append("<tr>")
        for start, end, text, _label in header_cells(n, line):
            out.append(f'<td colspan="{end - start + 1}">{escape(text)}</td>')
        out.append("</tr>")
    out.append('<tr bgcolor="#eeeeee">' + "".join(f'<td align="center"><b>{escape(c.header)}</b></td>'
                                                  for c in cols) + "</tr>")
    if not table.rows:
        out.append(f'<tr><td colspan="{n}"><i>{escape(table.empty_text)}</i></td></tr>')
    for row in table.rows[:max_rows]:
        cells = []
        for col, cell in zip(cols, row.cells):
            text = escape(fmt(cell.value, col.decimals))
            if cell.marker:
                text += f"<sup>({cell.marker})</sup>"
            if cell.bold or row.kind == "sum":
                text = f"<b>{text}</b>"
            numeric = col.numeric and isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool)
            cells.append(f'<td align="{"right" if numeric else "left"}">{text}</td>')
        out.append(("<tr bgcolor=\"#f6f6f6\">" if row.kind == "sum" else "<tr>") + "".join(cells) + "</tr>")
    if len(table.rows) > max_rows:
        out.append(f'<tr><td colspan="{n}"><i>… {len(table.rows) - max_rows} more rows</i></td></tr>')
    out.append("</table>")
    for k, text in enumerate(table.footnotes):
        out.append(f"<p><sup>({chr(ord('a') + k)})</sup> {escape(text)}</p>")
    for text in table.notes:
        out.append(f"<p><i>{escape(text)}</i></p>")
    out.append("</body></html>")
    return "".join(out)
