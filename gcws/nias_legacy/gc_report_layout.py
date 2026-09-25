"""Keep complete report text visible in Excel and in exported Word tables."""
from copy import copy
import math
import textwrap


def fit_report_rows(ws):
    names = [cell.column for cell in ws[5] if str(cell.value or '').strip().casefold() == 'name']
    for column in names:
        width = ws.column_dimensions[ws.cell(5, column).column_letter].width or 30
        for row in range(6, ws.max_row + 1):
            cell = ws.cell(row, column)
            if cell.value is None:
                continue
            alignment = copy(cell.alignment)
            alignment.wrap_text = True
            alignment.shrink_to_fit = False
            cell.alignment = alignment
            # Reserve space for proportional-font words and explicit line breaks.
            lines = sum(max(1, len(textwrap.wrap(line, width=max(8, int(width * .85)))) )
                        for line in str(cell.value).split('\n'))
            ws.row_dimensions[row].height = max(ws.row_dimensions[row].height or 15,
                                                lines * ((cell.font.sz or 8) * 1.4) + 5)
    cell = ws['B4']
    if cell.value:
        alignment = copy(cell.alignment)
        alignment.wrap_text = True
        cell.alignment = alignment
        ws.row_dimensions[4].height = max(ws.row_dimensions[4].height or 15,
                                        math.ceil(len(str(cell.value)) / 55) * 13 + 4)
