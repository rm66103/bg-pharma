# Process Document: Automated Medication Search and Allergy Filtering

## Overview

Automated search system for DailyMed (NIH) to find capsule or liquid medications that don't contain specific allergens. The system will paginate through search results, filter by form type, check inactive ingredients against an allergy list, and output qualified results.

## Target Website

- Base URL: `https://dailymed.nlm.nih.gov/dailymed/search.cfm`
- URL Parameters:
  - `labeltype=all`
  - `query={medication_name}`
  - `pagesize=200` (maximum)
  - `page={page_number}`

## Process Flow

### 1. Search Initialization

- Accept medication name as input (command line argument)
- Construct search URL with `pagesize=200` to minimize pagination
- Make initial request to search endpoint

### 2. Pagination and Result Collection

- Parse HTML response using BeautifulSoup4
- Extract all result links from the search results page
- Each result link should be the individual medication label page
- Detect pagination (presence of "next" link or page numbers)
- Iterate through all pages until no more results
- Store unique result URLs (deduplicate by URL)

### 3. Title/Form Type Filtering

- For each result URL:
  - Fetch the medication label page
  - Extract the medication title/name
  - Use AI (OpenAI) to analyze the title for form type
  - **Qualify**: Capsule, Liquid, Tablet (oral forms)
  - **Disqualify**: Cream, Ointment, Injection, Topical, etc.
  - Store qualified results with their titles and URLs

### 4. Active Page Validation

- Check for red warning banner: "this includes inactive NDC codes..."
- If present, disqualify this result (indicates inactive/invalid NDC)
- Continue to next step only if no warning present

### 5. Inactive Ingredients Extraction

- Locate "Inactive ingredients" section (likely a collapsible/dropdown)
- Use AI + BeautifulSoup4 to:
  - Find and expand the dropdown/collapsible section
  - Extract the full list of inactive ingredients
  - Parse ingredient names from the HTML structure

### 6. Allergy Checking

- Compare extracted inactive ingredients against allergy list:
  - eggs
  - corn
  - cornstarch
  - dextrose
  - lactose
  - whey
  - wheat
- If ANY allergen found in inactive ingredients → disqualify
- Only proceed if NO allergens present

### 7. Result Aggregation

- Collect all qualified medications with:
  - Medication name/title
  - Full URL to the label page
  - Form type (for verification)

### 8. Output Format

- Format results as markdown or console-friendly text
- Each result should include:
  - Medication name
  - URL
  - Form type (optional, for verification)
- Output options:
  - Console print (pretty-printed, copy-pastable)
  - Save to markdown file (`{medication_name}_results.md`)
  - Or both

## Technical Components

### Libraries Required

- `requests`: HTTP requests for web scraping
- `beautifulsoup4`: HTML parsing and element extraction
- `openai`: AI-powered content analysis and extraction
- `lxml`: Fast HTML parser for BeautifulSoup4
- Standard library: `argparse`, `re`, `json`, `typing`

### AI Prompt Strategy

- **Form Type Detection**: Prompt to analyze medication title and classify form type
- **Ingredient Extraction**: Prompt to locate and extract inactive ingredients list from HTML structure
- **Ingredient Parsing**: Parse comma-separated or list-formatted ingredients

### Error Handling

- Network errors: Retry logic with exponential backoff
- Missing elements: Graceful handling if dropdown/inactive ingredients section not found
- Rate limiting: Respectful delays between requests
- Invalid responses: Skip problematic results and log issues

### Performance Considerations

- Use `pagesize=200` to minimize requests
- Batch AI requests where possible
- Cache results to avoid re-processing
- Progress indicators for long-running searches

## Example Usage

```bash
# With OpenAI API key from environment variable
export OPENAI_API_KEY="your-key-here"
python search_medication.py hydrocortisone

# With OpenAI API key as argument
python search_medication.py "prednisone" --openai-key "your-key-here"

# Without OpenAI (uses fallback methods)
python search_medication.py hydrocortisone

# Custom output file
python search_medication.py hydrocortisone --output results.md
```

## Output Example

```
=== Search Results for: hydrocortisone ===

Qualified Medications (No Allergens, Capsule/Liquid Only):

1. Hydrocortisone Capsule 20mg
   URL: https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid=...
   Form: Capsule

2. Hydrocortisone Oral Suspension 10mg/5ml
   URL: https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid=...
   Form: Liquid

Total: 2 qualified results
```

## Implementation Details

### URL Normalization

The system normalizes URLs by extracting the `setid` parameter to avoid processing duplicate medications that may appear with different URL parameters.

### Fallback Methods

If OpenAI API is not available, the system falls back to:
- Regex-based form type detection
- BeautifulSoup-based ingredient extraction

These fallbacks are less accurate but allow the script to function without AI capabilities.

### Rate Limiting

The script includes respectful delays between requests:
- 1 second between pagination requests
- 0.5 seconds between individual medication page requests
- Exponential backoff for retries (2s, 4s, 8s)

## Future Enhancements (Optional)

- Batch processing: Accept list of medications from file
- Configuration file for allergy list (instead of hardcoded)
- Export to CSV for spreadsheet use
- Interactive mode with user prompts
- Progress bar for better UX
- Parallel processing for faster results

