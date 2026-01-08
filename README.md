# DailyMed Medication Search and Allergy Filter

Automated tool to search DailyMed (NIH) for capsule or liquid medications that don't contain specific allergens.

## Installation

1. Install Python 3.7 or higher
2. Install dependencies:

```bash
pip install -r requirements.txt
```

## Setup

For best results, set your OpenAI API key (optional but recommended):

```bash
export OPENAI_API_KEY="your-api-key-here"
```

Or pass it as a command-line argument using `--openai-key`.

**Note**: The script works without OpenAI API key using fallback methods, but AI-powered analysis is more accurate.

## Usage

```bash
python search_medication.py <medication_name>
```

### Examples

```bash
# Search for hydrocortisone
python search_medication.py hydrocortisone

# Search with spaces (use quotes)
python search_medication.py "prednisone 5mg"

# Specify custom output file
python search_medication.py hydrocortisone --output my_results.md

# With OpenAI API key
python search_medication.py hydrocortisone --openai-key "sk-..."
```

## What It Does

1. **Searches** DailyMed for the specified medication
2. **Paginates** through all search results automatically
3. **Filters** by form type (capsule/liquid only, excludes creams/injections)
4. **Validates** that pages don't have inactive NDC warnings
5. **Extracts** inactive ingredients from each medication label
6. **Checks** for allergens (eggs, corn, cornstarch, dextrose, lactose, whey, wheat)
7. **Returns** only medications that meet all criteria

## Output

Results are displayed in the console and saved to a markdown file:
- Console: Pretty-printed, copy-pastable format
- File: `{medication_name}_results.md` (or custom filename)

Each result includes:
- Medication name/title
- URL to the label page
- Form type

## Allergens Checked

The script automatically checks for these allergens in inactive ingredients:
- eggs
- corn
- cornstarch
- dextrose
- lactose
- whey
- wheat

## Troubleshooting

### "No OpenAI API key provided"

This is a warning, not an error. The script will use fallback methods (regex/BeautifulSoup) which are less accurate but still functional.

### "No results found"

- Check your internet connection
- Verify the medication name spelling
- Try a more generic search term (e.g., "prednisone" instead of "prednisone 5mg tablet")

### Slow performance

The script includes respectful delays between requests to avoid overwhelming the server. For 200+ results, expect the search to take several minutes.

## Notes

- The script respects rate limits with built-in delays
- Results are deduplicated by medication setid
- Progress is shown in the console as pages are processed
- Failed requests are automatically retried with exponential backoff

