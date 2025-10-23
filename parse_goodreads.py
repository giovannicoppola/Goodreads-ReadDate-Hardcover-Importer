"""Parse Goodreads data and export to TSV.

Usage:
  parse_goodreads.py <user_id> [--output=<file>] [--help]
  parse_goodreads.py (-h | --help)

Options:
  <user_id>           Goodreads user ID (e.g., 38810427-giovanni)
  --output=<file>     Output TSV file name [default: books.tsv]
  -h --help          Show this help message and exit

Examples:
  parse_goodreads.py 38810427-giovanni
  parse_goodreads.py 38810427-giovanni --output=my_books.tsv
"""

import csv
import json
import os
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from docopt import docopt


def get_text_or_default(element, default=""):
    """Extract text safely from an element."""
    return element.text.strip() if element else default


def convert_date(date_str):
    """Convert date to standard format."""
    try:
        return datetime.strptime(date_str, "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return date_str


def fetch_html(url, cookies=None):
    """Fetch HTML content from a URL with optional cookies."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.goodreads.com/',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'same-origin',
        'Sec-Fetch-User': '?1'
    }
    
    session = requests.Session()
    
    # Convert cookies to the format requests expects
    if cookies:
        cookie_dict = {}
        for cookie in cookies:
            if not cookie.get('hostOnly', False) or (cookie.get('domain', '').startswith('.goodreads.com') or cookie.get('domain', '') == 'www.goodreads.com'):
                cookie_dict[cookie['name']] = cookie['value']
        session.cookies.update(cookie_dict)
    
    try:
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.text
    except requests.exceptions.RequestException as e:
        raise


def process_review_row(row):
    """Process a single review row and return a dictionary of the data."""
    book_title = get_text_or_default(row.find("td", class_="field title").find("a"))
    date_started = get_text_or_default(
        row.find("td", class_="field date_started").find(
            "span", class_="date_started_value"
        )
    )
    date_read = get_text_or_default(
        row.find("td", class_="field date_read").find("span", class_="date_read_value")
    )
    goodreads_id = (
        row.find("div", class_="js-tooltipTrigger")["data-resource-id"]
        if row.find("div", class_="js-tooltipTrigger")
        else ""
    )

    # Convert dates to standard format
    date_started = convert_date(date_started)
    date_read = convert_date(date_read)

    # Additional fields
    author = get_text_or_default(row.find("td", class_="field author").find("a"))
    avg_rating = get_text_or_default(row.find("td", class_="field avg_rating"))
    num_pages = (
        get_text_or_default(
            row.find("td", class_="field num_pages").find("nobr")
        ).split()[0]
        if row.find("td", class_="field num_pages").find("nobr")
        else ""
    )

    return {
        "Goodreads ID": goodreads_id,
        "Book Title": book_title,
        "Date Started": date_started,
        "Date Read": date_read,
        "Author": author,
        "Average Rating": avg_rating,
        "Number of Pages": num_pages,
    }


def download_and_process_goodreads_data(user_id, output_file):
    """Download Goodreads data and process it directly to TSV."""
    base_url = f"https://www.goodreads.com/review/list/{user_id}?page={{}}&per_page=100&ref=nav_mybooks&utf8=%E2%9C%93"

    # Try to load cookies if they exist
    cookies = None
    if os.path.exists("cookies.json"):
        try:
            with open("cookies.json", "r") as f:
                cookies = json.load(f)
        except json.JSONDecodeError:
            print(
                "Warning: cookies.json exists but is not valid JSON. Proceeding without cookies."
            )

    # Open the TSV file for writing
    with open(output_file, "w", newline="", encoding="utf-8") as tsvfile:
        fieldnames = [
            "Goodreads ID",
            "Book Title",
            "Date Started",
            "Date Read",
            "Author",
            "Average Rating",
            "Number of Pages",
        ]
        writer = csv.DictWriter(tsvfile, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()

        # Fetch the first page to determine the total number of pages
        try:
            first_page_html = fetch_html(base_url.format(1), cookies)
            soup = BeautifulSoup(first_page_html, "html.parser")

            # Extract the total number of pages
            pagination = soup.find("div", id="reviewPagination")
            if pagination:
                total_pages = int(pagination.find_all("a")[-2].text.strip())
            else:
                print("Could not determine total pages. Processing first page only.")
                total_pages = 1

            # Process each page
            for page in range(1, total_pages + 1):
                print(f"Processing page {page} of {total_pages}...")
                html_content = fetch_html(base_url.format(page), cookies)
                soup = BeautifulSoup(html_content, "html.parser")

                # Find all review rows
                review_rows = soup.find_all("tr", class_="bookalike review")

                # Process each row and write to TSV
                for row in review_rows:
                    book_data = process_review_row(row)
                    writer.writerow(book_data)

                # Add a small delay to be respectful to the server
                time.sleep(1)

            print(f"Successfully processed {total_pages} pages of Goodreads data.")
            return True

        except requests.exceptions.HTTPError as e:
            print(f"Error downloading data: {e}")
            print("This might be due to:")
            print("1. Invalid user ID")
            print("2. Profile is private")
            print("3. Missing or invalid cookies")
            return False


def main():
    args = docopt(__doc__)

    if download_and_process_goodreads_data(args["<user_id>"], args["--output"]):
        print(f"Data successfully written to {args['--output']}")
        
        # Check if the Date Read column has any data
        try:
            with open(args["--output"], "r", encoding="utf-8") as tsvfile:
                reader = csv.DictReader(tsvfile, delimiter="\t")
                has_any_date_read = any(row.get("Date Read", "").strip() for row in reader)
                
                if not has_any_date_read:
                    print("\n⚠️  WARNING: The 'Date Read' column appears to be empty for all books.")
                    print("Please ensure the 'Date Read' column is visible in your Goodreads library view.")
                    print("To do this:")
                    print("  1. Go to your Goodreads library")
                    print("  2. Click 'edit' at the top of the columns")
                    print("  3. Make sure 'Date Read' is checked/visible")
                    print("  4. Re-run this script after making the column visible")
        except Exception as e:
            print(f"Warning: Could not verify Date Read column: {e}")
    else:
        print("Failed to process Goodreads data.")


if __name__ == "__main__":
    main()
