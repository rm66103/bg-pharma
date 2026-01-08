#!/usr/bin/env python3
"""
Automated medication search and allergy filtering for DailyMed (NIH).
Searches for capsule or liquid medications that don't contain specific allergens.
"""

import argparse
import json
import re
import time
import sys
from typing import List, Dict, Optional, Set
from urllib.parse import urlencode, urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from openai import OpenAI


# Allergy list to check against
ALLERGENS = [
    "eggs",
    "corn",
    "cornstarch",
    "dextrose",
    "lactose",
    "whey",
    "wheat"
]

# Base URL for DailyMed search
BASE_URL = "https://dailymed.nlm.nih.gov/dailymed/search.cfm"
MAX_PAGE_SIZE = 200

# Form types that qualify
QUALIFYING_FORMS = ["capsule", "liquid", "tablet", "oral", "suspension", "solution", "syrup"]
DISQUALIFYING_FORMS = ["cream", "ointment", "injection", "topical", "gel", "lotion", "spray", "patch"]

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds


class MedicationSearcher:
    def __init__(self, openai_api_key: Optional[str] = None):
        """Initialize the medication searcher."""
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        })
        self.openai_client = OpenAI(api_key=openai_api_key) if openai_api_key else None
        self.seen_urls: Set[str] = set()
        
    def _make_request(self, url: str, retries: int = MAX_RETRIES) -> Optional[requests.Response]:
        """Make HTTP request with retry logic."""
        for attempt in range(retries):
            try:
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                return response
            except requests.RequestException as e:
                if attempt < retries - 1:
                    wait_time = RETRY_DELAY * (.2 * attempt)
                    print(f"Request failed, retrying in {wait_time}s... ({attempt + 1}/{retries})")
                    time.sleep(wait_time)
                else:
                    print(f"Failed to fetch {url}: {e}")
                    return None
        return None
    
    def _get_search_url(self, medication_name: str, page: int = 1) -> str:
        """Construct search URL with parameters."""
        params = {
            'labeltype': 'all',
            'query': medication_name,
            'pagesize': MAX_PAGE_SIZE,
            'page': page
        }
        return f"{BASE_URL}?{urlencode(params)}"
    
    def _extract_result_urls(self, html: str) -> List[str]:
        """Extract medication label URLs from search results page."""
        soup = BeautifulSoup(html, 'lxml')
        urls = []
        
        # Find all links that point to medication label pages
        # DailyMed uses lookup.cfm with setid parameter
        for link in soup.find_all('a', href=True):
            href = link.get('href')
            if href and ('lookup.cfm' in href or 'setid=' in href):
                # Make absolute URL if relative
                if href.startswith('/'):
                    full_url = urljoin(BASE_URL, href)
                elif not href.startswith('http'):
                    full_url = urljoin(BASE_URL, href)
                else:
                    full_url = href
                    
                # Extract setid parameter to normalize URL
                parsed = urlparse(full_url)
                query_params = parse_qs(parsed.query)
                if 'setid' in query_params:
                    # Normalize URL by setid
                    normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?setid={query_params['setid'][0]}"
                    if normalized not in self.seen_urls:
                        urls.append(normalized)
                        self.seen_urls.add(normalized)
        
        # If no URLs found via lookup.cfm, try alternative patterns
        if not urls:
            # Look for any links with setid parameter
            for link in soup.find_all('a', href=True):
                href = link.get('href')
                if href and 'setid=' in href:
                    if href.startswith('/'):
                        full_url = urljoin(BASE_URL, href)
                    elif not href.startswith('http'):
                        full_url = urljoin(BASE_URL, href)
                    else:
                        full_url = href
                    
                    if full_url not in self.seen_urls:
                        urls.append(full_url)
                        self.seen_urls.add(full_url)
        
        return urls
    
    def _has_next_page(self, html: str, current_page: int) -> bool:
        """Check if there's a next page of results."""
        soup = BeautifulSoup(html, 'lxml')
        
        # Look for pagination links with "Next" text
        next_links = soup.find_all('a', href=True, string=re.compile(r'next|Next|>', re.I))
        
        # Check if any Next link is not disabled
        for link in next_links:
            parent = link.parent
            classes = parent.get('class', []) if parent else []
            if 'disabled' not in str(classes).lower():
                return True
        
        # Look for page number links - if we find a page number higher than current, there's a next page
        page_links = soup.find_all('a', href=re.compile(r'page=\d+'))
        for link in page_links:
            href = link.get('href', '')
            page_match = re.search(r'page=(\d+)', href)
            if page_match:
                page_num = int(page_match.group(1))
                if page_num > current_page:
                    return True
        
        # Check for "Next" or ">" in link text that might be in different elements
        for link in soup.find_all('a', href=re.compile(r'page=')):
            text = link.get_text(strip=True).lower()
            if 'next' in text or '>' in text or text.isdigit():
                classes = link.get('class', [])
                if 'disabled' not in str(classes).lower():
                    return True
            
        return False
    
    def collect_all_result_urls(self, medication_name: str) -> List[str]:
        """Collect all result URLs by paginating through search results."""
        all_urls = []
        page = 1
        
        print(f"Collecting search results for: {medication_name}")
        
        while True:
            search_url = self._get_search_url(medication_name, page)
            print(f"Fetching page {page}...")
            
            response = self._make_request(search_url)
            if not response:
                break
                
            page_urls = self._extract_result_urls(response.text)
            all_urls.extend(page_urls)
            print(f"Found {len(page_urls)} results on page {page} (total so far: {len(all_urls)})")
            
            # Check for next page
            if not self._has_next_page(response.text, page) or len(page_urls) == 0:
                break
                
            page += 1
            time.sleep(.1)  # Be respectful with requests
        
        print(f"Total unique results collected: {len(all_urls)}\n")
        return all_urls
    
    def _check_inactive_ndc_warning(self, soup: BeautifulSoup) -> bool:
        """Check if page has the red inactive NDC warning."""
        # Look for red warning text about inactive NDC codes
        warning_text = soup.find_all(string=re.compile(r'inactive.*NDC', re.I))
        
        # Check if it's in a red/warning styled element
        for text in warning_text:
            parent = text.parent
            if parent:
                # Check for red styling
                classes = parent.get('class', [])
                style = parent.get('style', '')
                
                # Look for red color indicators
                if ('red' in str(classes).lower() or 
                    'warning' in str(classes).lower() or
                    'error' in str(classes).lower() or
                    'red' in style.lower() or
                    'color:#' in style.lower() or
                    'color:red' in style.lower()):
                    return True
                
                # Also check parent elements
                grandparent = parent.parent
                if grandparent:
                    classes = grandparent.get('class', [])
                    style = grandparent.get('style', '')
                    if ('red' in str(classes).lower() or 
                        'warning' in str(classes).lower() or
                        'red' in style.lower()):
                        return True
        
        return False
    
    def _extract_medication_title(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract medication title/name from the label page."""
        # Try common title locations
        title_selectors = [
            'h1',
            '.drug-title',
            '.label-title',
            'title'
        ]
        
        for selector in title_selectors:
            elem = soup.select_one(selector)
            if elem:
                title = elem.get_text(strip=True)
                if title and len(title) > 5:  # Reasonable title length
                    return title
        
        # Fallback: use page title
        title_tag = soup.find('title')
        if title_tag:
            return title_tag.get_text(strip=True)
        
        return None
    
    def _analyze_form_type_ai(self, title: str) -> Dict[str, any]:
        """Use AI to analyze medication title for form type."""
        if not self.openai_client:
            # Fallback to regex if no OpenAI key
            return self._analyze_form_type_regex(title)
        
        prompt = f"""Analyze this medication name and determine if it's a capsule, liquid, or other oral form suitable for swallowing.

Medication name: "{title}"

Respond with ONLY a JSON object in this exact format:
{{"form_type": "capsule|liquid|tablet|other_oral|disqualify", "confidence": "high|medium|low", "reasoning": "brief explanation"}}

Qualifying forms: capsule, liquid, tablet, oral suspension, oral solution, syrup, chewable tablet
Disqualifying forms: cream, ointment, injection, topical, gel, lotion, spray, patch, eye drops, ear drops, nasal spray

If uncertain, choose "disqualify" to be safe."""

        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a medical information analyzer. Respond only with valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=150
            )
            
            result_text = response.choices[0].message.content.strip()
            # Extract JSON from response
            json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                return result
        except Exception as e:
            print(f"AI analysis failed: {e}, using fallback")
        
        return self._analyze_form_type_regex(title)
    
    def _analyze_form_type_regex(self, title: str) -> Dict[str, any]:
        """Fallback regex-based form type analysis."""
        title_lower = title.lower()
        
        # Check for disqualifying forms first
        for form in DISQUALIFYING_FORMS:
            if form in title_lower:
                return {
                    "form_type": "disqualify",
                    "confidence": "high",
                    "reasoning": f"Contains '{form}' in name"
                }
        
        # Check for qualifying forms
        for form in QUALIFYING_FORMS:
            if form in title_lower:
                return {
                    "form_type": form,
                    "confidence": "high",
                    "reasoning": f"Contains '{form}' in name"
                }
        
        return {
            "form_type": "unknown",
            "confidence": "low",
            "reasoning": "Could not determine form type"
        }
    
    def _extract_inactive_ingredients_ai(self, soup: BeautifulSoup) -> List[str]:
        """Use AI to extract inactive ingredients from the page."""
        if not self.openai_client:
            return self._extract_inactive_ingredients_bs4(soup)
        
        # Get relevant HTML sections
        page_text = soup.get_text(separator=' ', strip=True)
        
        # Find inactive ingredients section
        inactive_section = None
        for elem in soup.find_all(['div', 'section', 'span', 'p']):
            text = elem.get_text(strip=True)
            if re.search(r'inactive.*ingredient', text, re.I):
                inactive_section = elem
                break
        
        # If not found, search in all text
        if not inactive_section:
            # Look for collapsible sections
            for elem in soup.find_all(['div', 'section'], class_=re.compile(r'collapse|expand|dropdown|accordion', re.I)):
                text = elem.get_text()
                if re.search(r'inactive.*ingredient', text, re.I):
                    inactive_section = elem
                    break
        
        section_html = str(inactive_section) if inactive_section else soup.prettify()[:10000]  # Limit size
        
        prompt = f"""Extract the complete list of inactive ingredients from this medication label HTML.

Focus on finding the "Inactive ingredients" or "Inactive components" section. The ingredients are typically listed after this heading, possibly in a collapsible/dropdown section.

HTML excerpt:
{section_html[:5000]}

Respond with ONLY a JSON array of ingredient names in this exact format:
["ingredient1", "ingredient2", "ingredient3", ...]

If you cannot find inactive ingredients, return an empty array: []"""

        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a medical label parser. Extract ingredient lists from HTML. Respond only with valid JSON array."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=500
            )
            
            result_text = response.choices[0].message.content.strip()
            # Extract JSON array
            json_match = re.search(r'\[.*\]', result_text, re.DOTALL)
            if json_match:
                ingredients = json.loads(json_match.group())
                return [str(ing).strip().lower() for ing in ingredients if ing]
        except Exception as e:
            print(f"AI ingredient extraction failed: {e}, using fallback")
        
        return self._extract_inactive_ingredients_bs4(soup)
    
    def _extract_inactive_ingredients_bs4(self, soup: BeautifulSoup) -> List[str]:
        """Fallback BeautifulSoup-based ingredient extraction."""
        ingredients = []
        
        # Strategy 1: Find heading/strong text with "Inactive ingredients"
        for elem in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'b', 'span', 'div', 'p']):
            text = elem.get_text(strip=True)
            
            # Look for "Inactive ingredients" heading
            if re.search(r'inactive\s+(ingredients?|components?)', text, re.I):
                # Get the parent container
                container = elem.parent
                if container:
                    # Look for the content after the heading
                    # Could be in next sibling, or in children
                    siblings = container.find_next_siblings(['div', 'section', 'p', 'ul', 'ol', 'span'])
                    for sibling in siblings[:3]:  # Check first 3 siblings
                        sibling_text = sibling.get_text(separator=' ', strip=True)
                        if sibling_text and len(sibling_text) > 10:
                            # Extract ingredients from this sibling
                            parts = re.split(r'[,;•\n\r]+', sibling_text)
                            for part in parts:
                                part = part.strip()
                                # Remove common prefixes
                                part = re.sub(r'^(inactive\s+(ingredients?|components?)[:]\s*)', '', part, flags=re.I)
                                if part and len(part) > 2 and not re.match(r'^\d+$', part):
                                    ingredients.append(part.lower())
                    
                    # Also check children
                    children = container.find_all(['li', 'p', 'span', 'div'])
                    for child in children:
                        child_text = child.get_text(strip=True)
                        if child_text and len(child_text) > 2:
                            # Split by delimiters
                            parts = re.split(r'[,;•\n\r]+', child_text)
                            for part in parts:
                                part = part.strip()
                                part = re.sub(r'^(inactive\s+(ingredients?|components?)[:]\s*)', '', part, flags=re.I)
                                if part and len(part) > 2 and not re.match(r'^\d+$', part):
                                    ingredients.append(part.lower())
                
                # Strategy 2: Check if text itself contains the list
                if ',' in text or ';' in text:
                    # Extract the part after "Inactive ingredients:"
                    match = re.search(r'inactive\s+(ingredients?|components?)[:]\s*(.+)', text, re.I)
                    if match:
                        ingredients_text = match.group(2)
                        parts = re.split(r'[,;•\n\r]+', ingredients_text)
                        for part in parts:
                            part = part.strip()
                            if part and len(part) > 2 and not re.match(r'^\d+$', part):
                                ingredients.append(part.lower())
                break
        
        # Strategy 3: Look for collapsible/accordion sections
        if not ingredients:
            for elem in soup.find_all(['div', 'section'], class_=re.compile(r'collapse|expand|accordion|dropdown', re.I)):
                elem_text = elem.get_text()
                if re.search(r'inactive\s+(ingredients?|components?)', elem_text, re.I):
                    # Extract from this section
                    parts = re.split(r'[,;•\n\r]+', elem_text)
                    for part in parts:
                        part = part.strip()
                        part = re.sub(r'^.*?inactive.*?:?\s*', '', part, flags=re.I)
                        if part and len(part) > 2 and not re.match(r'^\d+$', part):
                            ingredients.append(part.lower())
        
        # Strategy 4: Look for list items with ingredient-like text
        if not ingredients:
            for li in soup.find_all('li'):
                text = li.get_text(strip=True)
                parent_text = ''
                if li.parent:
                    parent_text = li.parent.get_text()
                
                # If parent mentions inactive ingredients
                if re.search(r'inactive\s+(ingredients?|components?)', parent_text, re.I):
                    if text and len(text) > 2 and not re.match(r'^\d+$', text):
                        ingredients.append(text.lower())
        
        # Clean and deduplicate
        cleaned = []
        for ing in ingredients:
            ing = re.sub(r'[^\w\s-]', '', ing)  # Remove special chars except hyphens
            ing = ing.strip()
            if ing and len(ing) > 2:
                cleaned.append(ing)
        
        return list(set(cleaned))  # Deduplicate
    
    def _check_allergies(self, ingredients: List[str]) -> bool:
        """Check if any allergens are present in ingredients list."""
        ingredients_text = ' '.join(ingredients).lower()
        
        for allergen in ALLERGENS:
            if allergen.lower() in ingredients_text:
                return True
        
        return False
    
    def process_medication_page(self, url: str) -> Optional[Dict]:
        """Process a single medication label page and return qualified result."""
        response = self._make_request(url)
        if not response:
            return None
        
        soup = BeautifulSoup(response.text, 'lxml')
        
        # Step 1: Check for inactive NDC warning
        if self._check_inactive_ndc_warning(soup):
            return None
        
        # Step 2: Extract title
        title = self._extract_medication_title(soup)
        if not title:
            return None
        
        # Step 3: Analyze form type
        form_analysis = self._analyze_form_type_ai(title)
        if form_analysis.get('form_type') in ['disqualify', 'unknown']:
            return None
        
        form_type = form_analysis.get('form_type', 'unknown')
        
        # Step 4: Extract inactive ingredients
        inactive_ingredients = self._extract_inactive_ingredients_ai(soup)
        
        # Step 5: Check for allergens
        if self._check_allergies(inactive_ingredients):
            return None
        
        # Qualified!
        return {
            'title': title,
            'url': url,
            'form_type': form_type,
            'inactive_ingredients': inactive_ingredients
        }
    
    def search_medication(self, medication_name: str) -> List[Dict]:
        """Main search method that orchestrates the entire process."""
        print(f"\n{'='*60}")
        print(f"Searching for: {medication_name}")
        print(f"{'='*60}\n")
        
        # Collect all result URLs
        result_urls = self.collect_all_result_urls(medication_name)
        
        if not result_urls:
            print("No results found.")
            return []
        
        # Process each result
        qualified_results = []
        total = len(result_urls)
        
        print(f"Processing {total} medication pages...\n")
        
        for i, url in enumerate(result_urls, 1):
            print(f"[{i}/{total}] Processing: {url[:80]}...", end=' ')
            
            result = self.process_medication_page(url)
            
            if result:
                qualified_results.append(result)
                print(f"✓ QUALIFIED ({result['form_type']})")
            else:
                print("✗ Disqualified")
            
            time.sleep(0.1)  # Be respectful
        
        return qualified_results
    
    def format_results(self, medication_name: str, results: List[Dict]) -> str:
        """Format results for output (email-friendly format)."""
        output = []
        output.append(f"Search Results for: {medication_name}")
        output.append("=" * 60)
        output.append("")
        output.append("Qualified Medications (No Allergens, Capsule/Liquid Only):")
        output.append("")
        
        if not results:
            output.append("No qualified medications found.")
            output.append("")
        else:
            for i, result in enumerate(results, 1):
                output.append(f"{i}. {result['title']}")
                output.append(f"   {result['url']}")
                if result.get('form_type'):
                    output.append(f"   Form: {result['form_type'].title()}")
                output.append("")
            
            output.append(f"Total: {len(results)} qualified result(s)")
            output.append("")
        
        return "\n".join(output)
    
    def save_results(self, medication_name: str, results: List[Dict], filename: Optional[str] = None):
        """Save results to markdown file."""
        if filename is None:
            # Sanitize medication name for filename
            safe_name = re.sub(r'[^\w\s-]', '', medication_name).strip().replace(' ', '_')
            filename = f"{safe_name}_results.md"
        
        content = self.format_results(medication_name, results)
        
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(content)
        
        print(f"\nResults saved to: {filename}")


def main():
    parser = argparse.ArgumentParser(
        description='Search DailyMed for medications without specific allergens'
    )
    parser.add_argument(
        'medication',
        help='Name of the medication to search for'
    )
    parser.add_argument(
        '--openai-key',
        help='OpenAI API key (or set OPENAI_API_KEY environment variable)',
        default=None
    )
    parser.add_argument(
        '--output',
        help='Output filename for results (default: {medication}_results.md)',
        default=None
    )
    
    args = parser.parse_args()
    
    # Get OpenAI key from args or environment
    openai_key = args.openai_key or None
    if not openai_key:
        import os
        openai_key = os.getenv('OPENAI_API_KEY')
    
    if not openai_key:
        print("Warning: No OpenAI API key provided. Using fallback methods (less accurate).")
        print("Set OPENAI_API_KEY environment variable or use --openai-key flag.\n")
    
    # Initialize searcher
    searcher = MedicationSearcher(openai_api_key=openai_key)
    
    # Perform search
    results = searcher.search_medication(args.medication)
    
    # Format and display results
    output_text = searcher.format_results(args.medication, results)
    print("\n" + "="*60)
    print(output_text)
    print("="*60)
    
    # Save results
    searcher.save_results(args.medication, results, args.output)
    
    return 0 if results else 1


if __name__ == '__main__':
    sys.exit(main())

