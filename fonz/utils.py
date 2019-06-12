import re
from typing import List, Sequence
from urllib.parse import urljoin


def compose_url(base_url: str, path: List) -> str:
    if not isinstance(path, list):
        raise TypeError("URL path must be a list")
    parts = [base_url] + path
    url = "/".join(str(part).strip("/") for part in parts)
    return url


def mark_line(lines: Sequence, line_number: int, char: str = "*") -> List:
    """For a list of strings, mark a specified line with a prepended character."""
    marked = []
    for i, line in enumerate(lines):
        if i == line_number:
            marked.append(char + " " + line)
        else:
            marked.append("| " + line)
    return marked


def extract_sql_context(sql: str, line_number: int, window_size: int = 2) -> str:
    """Extract a line of SQL with a specified amount of surrounding context."""
    split = sql.split("\n")
    line_number -= 1  # Align with array indexing
    line_start = line_number - window_size
    line_end = line_number + (window_size + 1)
    line_start = line_start if line_start >= 0 else 0
    line_end = line_end if line_end <= len(split) else len(split)

    selected_lines = split[line_start:line_end]
    marked = mark_line(selected_lines, line_number=window_size)
    context = "\n".join(marked)
    return context


def parse_error_line_number(error_message: str) -> int:
    """Extract the line number for a SQL error from the error message."""
    BQ_LINE_NUM_PATTERN = r"at \[(\d+):\d+\]"
    try:
        line_number = re.findall(BQ_LINE_NUM_PATTERN, error_message)[0]
    except IndexError:
        pass  # Insert patterns for other data warehouses
    else:
        line_number = int(line_number)

    return line_number
