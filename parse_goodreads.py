"""Parse Goodreads data and export to TSV.

Usage:
  parse_goodreads.py <user_id> [--output=<file>] [--cookies=<file>] [--delay=<seconds>] [--help]
  parse_goodreads.py (-h | --help)

Options:
  <user_id>           Goodreads user ID (e.g., 38810427-giovanni)
  --output=<file>     Output TSV file name [default: books.tsv]
  --cookies=<file>    JSON file with exported Goodreads cookies [default: cookies.json]
  --delay=<seconds>   Seconds to wait between page requests [default: 1]
  -h --help           Show this help message and exit

Examples:
  parse_goodreads.py 38810427-giovanni
  parse_goodreads.py 38810427-giovanni --output=my_books.tsv
"""

import csv
import json
import os
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from docopt import docopt

FIELDNAMES = [
    "Goodreads ID",
    "Book Title",
    "Date Started",
    "Date Read",
    "Author",
    "Average Rating",
    "Number of Pages",
]

# Goodreads renders unset dates as this literal string.
_UNSET_DATE_MARKERS = {"not set", "unknown", "-"}

# Date layouts Goodreads uses in the library table.
_INPUT_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%b %Y", "%B %Y")


def get_text_or_default(element, default=""):
    """Extract text safely from an element."""
    return element.text.strip() if element else default


def cell_text(row, field, span_class=None, tag=None):
    """Return the text of a `td.field <field>` cell, or "" if it is absent.

    Goodreads only renders the columns the user has made visible, so any cell
    may legitimately be missing from the markup. Returning "" keeps a single
    hidden column from aborting the whole export.
    """
    cell = row.find("td", class_=f"field {field}")
    if cell is None:
        return ""
    if span_class is not None:
        return get_text_or_default(cell.find("span", class_=span_class))
    if tag is not None:
        return get_text_or_default(cell.find(tag))
    return get_text_or_default(cell)


def convert_date(date_str):
    """Convert a Goodreads date to ISO (yyyy-mm-dd) format.

    Month-only dates ("Mar 2020") are anchored to the first of the month.
    Placeholders such as "not set" become "" so downstream code can treat
    them as missing rather than as an unparseable date.
    """
    date_str = (date_str or "").strip()
    if not date_str or date_str.lower() in _UNSET_DATE_MARKERS:
        return ""

    for fmt in _INPUT_DATE_FORMATS:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str


def load_cookies(cookies_file):
    """Load exported browser cookies, keeping only the Goodreads ones."""
    if not os.path.exists(cookies_file):
        return None

    try:
        with open(cookies_file, "r", encoding="utf-8") as f:
            raw_cookies = json.load(f)
    except json.JSONDecodeError:
        print(
            f"Warning: {cookies_file} exists but is not valid JSON. "
            "Proceeding without cookies."
        )
        return None
    except OSError as e:
        print(f"Warning: could not read {cookies_file} ({e}). Proceeding without cookies.")
        return None

    if not isinstance(raw_cookies, list):
        print(
            f"Warning: {cookies_file} should contain a JSON list of cookies. "
            "Proceeding without cookies."
        )
        return None

    cookie_dict = {}
    for cookie in raw_cookies:
        if not isinstance(cookie, dict) or "name" not in cookie or "value" not in cookie:
            continue
        domain = cookie.get("domain", "").lstrip(".").lower()
        # Only ever send Goodreads cookies to Goodreads. Cookie exports often
        # contain every cookie in the browser profile.
        if domain == "goodreads.com" or domain.endswith(".goodreads.com"):
            cookie_dict[cookie["name"]] = cookie["value"]

    if not cookie_dict:
        print(f"Warning: no goodreads.com cookies found in {cookies_file}.")
        return None

    print(f"Loaded {len(cookie_dict)} Goodreads cookies from {cookies_file}.")
    return cookie_dict


def make_session(cookies=None):
    """Build a requests session with browser-like headers and optional cookies."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.goodreads.com/",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        }
    )
    if cookies:
        session.cookies.update(cookies)
    return session


def fetch_html(session, url):
    """Fetch HTML content from a URL, raising on HTTP errors."""
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return response.text


def process_review_row(row):
    """Process a single review row and return a dictionary of the data."""
    tooltip = row.find("div", class_="js-tooltipTrigger")
    goodreads_id = tooltip.get("data-resource-id", "") if tooltip else ""

    num_pages = cell_text(row, "num_pages", tag="nobr")
    # "320 pp" -> "320"
    num_pages = num_pages.split()[0] if num_pages else ""

    return {
        "Goodreads ID": goodreads_id,
        "Book Title": cell_text(row, "title", tag="a"),
        "Date Started": convert_date(cell_text(row, "date_started", span_class="date_started_value")),
        "Date Read": convert_date(cell_text(row, "date_read", span_class="date_read_value")),
        "Author": cell_text(row, "author", tag="a"),
        "Average Rating": cell_text(row, "avg_rating"),
        "Number of Pages": num_pages,
    }


def find_total_pages(soup):
    """Determine the number of library pages from the pagination widget."""
    pagination = soup.find("div", id="reviewPagination")
    if not pagination:
        return None

    page_numbers = []
    for link in pagination.find_all("a"):
        text = link.text.strip()
        if text.isdigit():
            page_numbers.append(int(text))
    return max(page_numbers) if page_numbers else None


def download_and_process_goodreads_data(user_id, output_file, cookies_file="cookies.json", delay=1.0):
    """Download Goodreads data and process it directly to TSV."""
    base_url = (
        f"https://www.goodreads.com/review/list/{user_id}"
        "?page={}&per_page=100&ref=nav_mybooks&utf8=%E2%9C%93"
    )

    session = make_session(load_cookies(cookies_file))

    # Fetch the first page before touching the output file, so a failed run
    # never truncates a previously good export.
    try:
        print("Processing page 1...")
        first_page_html = fetch_html(session, base_url.format(1))
    except requests.exceptions.HTTPError as e:
        print(f"Error downloading data: {e}")
        print("This might be due to:")
        print("1. Invalid user ID")
        print("2. Profile is private")
        print("3. Missing or invalid cookies")
        return False
    except requests.exceptions.RequestException as e:
        print(f"Network error downloading data: {e}")
        return False

    soup = BeautifulSoup(first_page_html, "html.parser")
    total_pages = find_total_pages(soup)
    if total_pages is None:
        print("Could not determine total pages. Processing first page only.")
        total_pages = 1

    rows_written = 0
    with open(output_file, "w", newline="", encoding="utf-8") as tsvfile:
        writer = csv.DictWriter(tsvfile, fieldnames=FIELDNAMES, delimiter="\t")
        writer.writeheader()

        for page in range(1, total_pages + 1):
            if page > 1:
                print(f"Processing page {page} of {total_pages}...")
                # Be respectful to the server between page requests.
                time.sleep(delay)
                try:
                    html_content = fetch_html(session, base_url.format(page))
                except requests.exceptions.RequestException as e:
                    print(f"Error downloading page {page}: {e}")
                    print("Stopping early; the TSV contains the pages fetched so far.")
                    break
                soup = BeautifulSoup(html_content, "html.parser")

            for row in soup.find_all("tr", class_="bookalike review"):
                try:
                    book = process_review_row(row)
                except Exception as e:  # one malformed row must not kill the export
                    print(f"Warning: skipping a malformed row on page {page} ({e}).")
                    continue
                if not book["Goodreads ID"] and not book["Book Title"]:
                    continue  # nothing identifiable in this row
                writer.writerow(book)
                rows_written += 1

    print(f"Successfully processed {rows_written} books from {total_pages} page(s).")
    return True


def warn_if_no_read_dates(output_file):
    """Warn when the exported file has no Date Read values at all."""
    try:
        with open(output_file, "r", encoding="utf-8") as tsvfile:
            reader = csv.DictReader(tsvfile, delimiter="\t")
            if any((row.get("Date Read") or "").strip() for row in reader):
                return
    except OSError as e:
        print(f"Warning: Could not verify Date Read column: {e}")
        return

    print("\n⚠️  WARNING: The 'Date Read' column appears to be empty for all books.")
    print("Please ensure the 'Date Read' column is visible in your Goodreads library view.")
    print("To do this:")
    print("  1. Go to your Goodreads library")
    print("  2. Click 'edit' at the top of the columns")
    print("  3. Make sure 'Date Read' is checked/visible")
    print("  4. Re-run this script after making the column visible")


def main():
    args = docopt(__doc__)

    try:
        delay = float(args["--delay"])
    except ValueError:
        print(f"Error: --delay must be a number, got {args['--delay']!r}")
        return 2
    if delay < 0:
        print("Error: --delay must not be negative.")
        return 2

    output_file = args["--output"]
    if not download_and_process_goodreads_data(
        args["<user_id>"], output_file, args["--cookies"], delay
    ):
        print("Failed to process Goodreads data.")
        return 1

    print(f"Data successfully written to {output_file}")
    warn_if_no_read_dates(output_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
