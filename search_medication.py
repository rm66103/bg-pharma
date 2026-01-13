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
from typing import List, Dict, Optional, Set, Tuple
from urllib.parse import urlencode, urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from openai import OpenAI


# Allergy list to check against
ALLERGENS = [
    "eggs",
    "corn",
    "cornstarch",
    "corn starch",
    "dextrose",
    "lactose",
    "whey",
    "wheat",
    "xylitol",
    "wheat",
    "gluten",
    "barley",
    "oats",
    "corn",
    "cornstarch",
    "dextrose",
    "lactose",
    "casein",
    "whey",
    "nuts",
    "eggs",
    "xylitol",
    "sorbitol"
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
    def __init__(self, openai_api_key: Optional[str] = None, verbose: bool = False):
        """Initialize the medication searcher."""
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        })
        self.openai_client = OpenAI(api_key=openai_api_key) if openai_api_key else None
        self.seen_urls: Set[str] = set()
        self.verbose = verbose
        
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
    
    def _extract_result_urls(self, html: str) -> Tuple[List[str], Dict]:
        """Extract medication label URLs from search results page. Returns (urls, verbose_info)."""
        soup = BeautifulSoup(html, 'lxml')
        urls = []
        verbose_info = {
            'total_links': 0,
            'lookup_cfm_links': 0,
            'setid_links': 0,
            'normalized_urls': 0,
            'duplicates_skipped': 0
        }
        
        all_links = soup.find_all('a', href=True)
        verbose_info['total_links'] = len(all_links)
        
        # Find all links that point to medication label pages
        # DailyMed uses lookup.cfm with setid parameter
        for link in all_links:
            href = link.get('href')
            if href and ('lookup.cfm' in href or 'setid=' in href):
                if 'lookup.cfm' in href:
                    verbose_info['lookup_cfm_links'] += 1
                if 'setid=' in href:
                    verbose_info['setid_links'] += 1
                    
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
                        verbose_info['normalized_urls'] += 1
                    else:
                        verbose_info['duplicates_skipped'] += 1
        
        # If no URLs found via lookup.cfm, try alternative patterns
        if not urls:
            # Look for any links with setid parameter
            for link in all_links:
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
                        verbose_info['normalized_urls'] += 1
                    else:
                        verbose_info['duplicates_skipped'] += 1
        
        return urls, verbose_info
    
    def _has_next_page(self, html: str, current_page: int) -> Tuple[bool, Dict]:
        """Check if there's a next page of results. Returns (has_next, verbose_info)."""
        soup = BeautifulSoup(html, 'lxml')
        verbose_info = {
            'next_links_found': 0,
            'next_links_disabled': 0,
            'page_links_found': 0,
            'higher_page_found': None,
            'method_used': None
        }
        
        # Look for pagination links with "Next" text
        next_links = soup.find_all('a', href=True, string=re.compile(r'next|Next|>', re.I))
        verbose_info['next_links_found'] = len(next_links)
        
        # Check if any Next link is not disabled
        for link in next_links:
            parent = link.parent
            classes = parent.get('class', []) if parent else []
            if 'disabled' not in str(classes).lower():
                verbose_info['method_used'] = 'next_link_text'
                return True, verbose_info
            else:
                verbose_info['next_links_disabled'] += 1
        
        # Look for page number links - if we find a page number higher than current, there's a next page
        page_links = soup.find_all('a', href=re.compile(r'page=\d+'))
        verbose_info['page_links_found'] = len(page_links)
        for link in page_links:
            href = link.get('href', '')
            page_match = re.search(r'page=(\d+)', href)
            if page_match:
                page_num = int(page_match.group(1))
                if page_num > current_page:
                    verbose_info['higher_page_found'] = page_num
                    verbose_info['method_used'] = 'page_number_higher'
                    return True, verbose_info
        
        # Check for "Next" or ">" in link text that might be in different elements
        for link in soup.find_all('a', href=re.compile(r'page=')):
            text = link.get_text(strip=True).lower()
            if 'next' in text or '>' in text or text.isdigit():
                classes = link.get('class', [])
                if 'disabled' not in str(classes).lower():
                    verbose_info['method_used'] = 'page_link_text'
                    return True, verbose_info
            
        verbose_info['method_used'] = 'none'
        return False, verbose_info
    
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
                
            page_urls, url_info = self._extract_result_urls(response.text)
            all_urls.extend(page_urls)
            print(f"Found {len(page_urls)} results on page {page} (total so far: {len(all_urls)})")
            if self.verbose:
                print(f"  URL extraction details: {url_info['total_links']} total links, "
                      f"{url_info['lookup_cfm_links']} lookup.cfm links, "
                      f"{url_info['setid_links']} setid links, "
                      f"{url_info['duplicates_skipped']} duplicates skipped")
            
            # Check for next page
            has_next, pagination_info = self._has_next_page(response.text, page)
            if self.verbose:
                print(f"  Pagination: has_next={has_next}, method={pagination_info.get('method_used', 'unknown')}")
            if not has_next or len(page_urls) == 0:
                break
                
            page += 1
            time.sleep(.1)  # Be respectful with requests
        
        print(f"Total unique results collected: {len(all_urls)}\n")
        return all_urls
    
    def _check_inactive_ndc_warning(self, soup: BeautifulSoup) -> Tuple[bool, Dict]:
        """Check if page has the red inactive NDC warning. Returns (found, verbose_info)."""
        verbose_info = {
            'detection_method': None,
            'inactive_ndc_tag_found': False,
            'warning_text_matches': 0,
            'elements_checked': 0,
            'red_styled_found': False,
            'warning_class_found': False,
            'details': []
        }
        
        # PRIMARY CHECK: Look for elements with the inactive-ndc-tag class (most reliable)
        inactive_ndc_tags = soup.find_all(class_=re.compile(r'inactive-ndc-tag', re.I))
        if inactive_ndc_tags:
            verbose_info['detection_method'] = 'inactive_ndc_tag_class'
            verbose_info['inactive_ndc_tag_found'] = True
            for tag in inactive_ndc_tags:
                classes = tag.get('class', [])
                text = tag.get_text(strip=True)
                verbose_info['details'].append({
                    'element': 'inactive-ndc-tag',
                    'classes': list(classes) if isinstance(classes, list) else str(classes),
                    'text': text[:100] if text else None
                })
            return True, verbose_info
        
        # FALLBACK: Look for red warning text about inactive NDC codes
        warning_text = soup.find_all(string=re.compile(r'inactive.*NDC', re.I))
        verbose_info['warning_text_matches'] = len(warning_text)
        
        # Check if it's in a red/warning styled element
        for text in warning_text:
            parent = text.parent
            if parent:
                verbose_info['elements_checked'] += 1
                # Check for red styling
                classes = parent.get('class', [])
                style = parent.get('style', '')
                
                # Look for red color indicators
                is_red = ('red' in str(classes).lower() or 
                         'warning' in str(classes).lower() or
                         'error' in str(classes).lower() or
                         'red' in style.lower() or
                         'color:#' in style.lower() or
                         'color:red' in style.lower())
                
                if is_red:
                    verbose_info['detection_method'] = 'red_styled_text'
                    verbose_info['red_styled_found'] = True
                    verbose_info['warning_class_found'] = 'warning' in str(classes).lower() or 'error' in str(classes).lower()
                    verbose_info['details'].append({
                        'text_snippet': text.strip()[:100],
                        'classes': list(classes) if isinstance(classes, list) else str(classes),
                        'style': style[:100] if style else None
                    })
                    return True, verbose_info
                
                # Also check parent elements
                grandparent = parent.parent
                if grandparent:
                    verbose_info['elements_checked'] += 1
                    classes = grandparent.get('class', [])
                    style = grandparent.get('style', '')
                    is_red = ('red' in str(classes).lower() or 
                             'warning' in str(classes).lower() or
                             'red' in style.lower())
                    if is_red:
                        verbose_info['detection_method'] = 'red_styled_parent_text'
                        verbose_info['red_styled_found'] = True
                        verbose_info['warning_class_found'] = 'warning' in str(classes).lower()
                        verbose_info['details'].append({
                            'text_snippet': text.strip()[:100],
                            'parent_classes': list(classes) if isinstance(classes, list) else str(classes),
                            'parent_style': style[:100] if style else None
                        })
                        return True, verbose_info
        
        verbose_info['detection_method'] = 'none'
        return False, verbose_info
    
    def _extract_medication_title(self, soup: BeautifulSoup) -> Tuple[Optional[str], Dict]:
        """Extract medication title/name from the label page. Returns (title, verbose_info)."""
        # Try common title locations
        title_selectors = [
            'h1',
            '.drug-title',
            '.label-title',
            'title'
        ]
        
        verbose_info = {
            'selectors_tried': [],
            'found_in': None,
            'title_text': None
        }
        
        for selector in title_selectors:
            elem = soup.select_one(selector)
            if elem:
                title = elem.get_text(strip=True)
                verbose_info['selectors_tried'].append({
                    'selector': selector,
                    'found': True,
                    'text_length': len(title) if title else 0,
                    'text_preview': title[:100] if title else None
                })
                if title and len(title) > 5:  # Reasonable title length
                    verbose_info['found_in'] = selector
                    verbose_info['title_text'] = title
                    return title, verbose_info
            else:
                verbose_info['selectors_tried'].append({
                    'selector': selector,
                    'found': False
                })
        
        # Fallback: use page title
        title_tag = soup.find('title')
        if title_tag:
            title = title_tag.get_text(strip=True)
            verbose_info['selectors_tried'].append({
                'selector': 'title (fallback)',
                'found': True,
                'text_length': len(title) if title else 0,
                'text_preview': title[:100] if title else None
            })
            if title:
                verbose_info['found_in'] = 'title (fallback)'
                verbose_info['title_text'] = title
                return title, verbose_info
        else:
            verbose_info['selectors_tried'].append({
                'selector': 'title (fallback)',
                'found': False
            })
        
        return None, verbose_info
    
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
    
    def _extract_inactive_ingredients_ai(self, soup: BeautifulSoup) -> Tuple[List[str], Dict]:
        """Use AI to extract inactive ingredients from the page. Returns (ingredients, verbose_info)."""
        verbose_info = {
            'method': 'ai' if self.openai_client else 'bs4_fallback',
            'ai_used': False,
            'fallback_reason': None
        }
        
        if not self.openai_client:
            verbose_info['fallback_reason'] = 'no_openai_client'
            ingredients, bs4_info = self._extract_inactive_ingredients_bs4(soup)
            verbose_info.update(bs4_info)
            return ingredients, verbose_info
        
        # Get relevant HTML sections
        page_text = soup.get_text(separator=' ', strip=True)
        
        # Find inactive ingredients section - prioritize tables
        inactive_section = None
        
        # First, look for tables with "Inactive Ingredients" heading
        for table in soup.find_all('table'):
            table_text = table.get_text()
            if re.search(r'inactive\s+(ingredients?|components?)', table_text, re.I):
                inactive_section = table
                break
        
        # If not found, look in div/section/span/p elements
        if not inactive_section:
            for elem in soup.find_all(['div', 'section', 'span', 'p']):
                text = elem.get_text(strip=True)
                if re.search(r'inactive.*ingredient', text, re.I):
                    inactive_section = elem
                    break
        
        # If still not found, look for collapsible sections
        if not inactive_section:
            for elem in soup.find_all(['div', 'section'], class_=re.compile(r'collapse|expand|dropdown|accordion', re.I)):
                text = elem.get_text()
                if re.search(r'inactive.*ingredient', text, re.I):
                    inactive_section = elem
                    break
        
        # Get a larger HTML snippet - include parent context if it's a table
        if inactive_section:
            if inactive_section.name == 'table':
                # For tables, include the table and its parent container
                parent = inactive_section.parent
                if parent:
                    section_html = str(parent)[:20000]  # Larger limit for tables
                else:
                    section_html = str(inactive_section)[:15000]
            else:
                # For other elements, include siblings and parent context
                parent = inactive_section.parent
                if parent:
                    section_html = str(parent)[:15000]
                else:
                    section_html = str(inactive_section)[:10000]
        else:
            section_html = soup.prettify()[:15000]  # Increased limit
        
        prompt = f"""Extract the complete list of inactive ingredients from this medication label HTML.

Focus on finding the "Inactive ingredients" or "Inactive components" section. The ingredients may be:
1. In a table with rows containing ingredient names (often in <strong> tags or <td> cells)
2. In a list (ul/ol) with list items
3. In a collapsible/dropdown section
4. In plain text after the heading, comma or semicolon separated

Look for ingredient names, ignoring UNII codes (things like "UNII: XF417D3PSL") and strength values.

HTML excerpt:
{section_html[:8000]}

Respond with ONLY a JSON array of ingredient names in this exact format:
["ingredient1", "ingredient2", "ingredient3", ...]

Extract only the actual ingredient names, cleaned of extra text like UNII codes. If you cannot find inactive ingredients, return an empty array: []"""

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
                result = [str(ing).strip().lower() for ing in ingredients if ing]
                verbose_info['ai_used'] = True
                verbose_info['ingredients_count'] = len(result)
                return result, verbose_info
        except Exception as e:
            verbose_info['fallback_reason'] = f'ai_extraction_error: {str(e)}'
            if self.verbose:
                print(f"AI ingredient extraction failed: {e}, using fallback")
        
        ingredients, bs4_info = self._extract_inactive_ingredients_bs4(soup)
        verbose_info.update(bs4_info)
        return ingredients, verbose_info
    
    def _extract_inactive_ingredients_bs4(self, soup: BeautifulSoup) -> Tuple[List[str], Dict]:
        """Fallback BeautifulSoup-based ingredient extraction. Returns (ingredients, verbose_info)."""
        ingredients = []
        verbose_info = {
            'method': 'bs4',
            'strategies_tried': [],
            'strategy_used': None,
            'heading_elements_found': 0,
            'ingredients_count': 0
        }
        
        # Strategy 0: Look for table-based ingredient lists (most common in DailyMed)
        verbose_info['strategies_tried'].append({'strategy': 0, 'name': 'table_based_extraction'})
        for table in soup.find_all('table'):
            table_text = table.get_text()
            if re.search(r'inactive\s+(ingredients?|components?)', table_text, re.I):
                # Found a table with inactive ingredients heading
                # Extract from table cells - look for strong tags or td elements
                rows = table.find_all('tr')
                for row in rows:
                    # Skip header rows
                    if row.find(['th'], string=re.compile(r'inactive|ingredient|name', re.I)):
                        continue
                    
                    # Look for ingredient name in strong tags or first td
                    strong_tag = row.find('strong')
                    if strong_tag:
                        ing_text = strong_tag.get_text(strip=True)
                        # Remove UNII codes and extra info in parentheses
                        ing_text = re.sub(r'\s*\(UNII:[^)]+\)', '', ing_text)
                        ing_text = ing_text.strip()
                        if ing_text and len(ing_text) > 2:
                            ingredients.append(ing_text.lower())
                    else:
                        # Try first td cell
                        first_td = row.find('td')
                        if first_td:
                            ing_text = first_td.get_text(strip=True)
                            # Remove UNII codes
                            ing_text = re.sub(r'\s*\(UNII:[^)]+\)', '', ing_text)
                            ing_text = re.sub(r'\s*UNII:\s*\S+', '', ing_text)
                            ing_text = ing_text.strip()
                            if ing_text and len(ing_text) > 2 and not re.match(r'^\d+$', ing_text):
                                ingredients.append(ing_text.lower())
                
                if ingredients:
                    verbose_info['strategy_used'] = 0
                    # Clean and deduplicate
                    cleaned = []
                    for ing in ingredients:
                        ing = re.sub(r'[^\w\s-]', '', ing)  # Remove special chars except hyphens
                        ing = ing.strip()
                        if ing and len(ing) > 2:
                            cleaned.append(ing)
                    final_ingredients = list(set(cleaned))
                    verbose_info['ingredients_count'] = len(final_ingredients)
                    return final_ingredients, verbose_info
        
        # Strategy 1: Find heading/strong text with "Inactive ingredients"
        verbose_info['strategies_tried'].append({'strategy': 1, 'name': 'heading_text_with_parent_siblings'})
        heading_elements = soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'b', 'span', 'div', 'p'])
        
        for elem in heading_elements:
            text = elem.get_text(strip=True)
            
            # Look for "Inactive ingredients" heading
            if re.search(r'inactive\s+(ingredients?|components?)', text, re.I):
                verbose_info['heading_elements_found'] += 1
                verbose_info['strategy_used'] = 1
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
                    verbose_info['strategies_tried'].append({'strategy': 2, 'name': 'text_contains_list'})
                    # Extract the part after "Inactive ingredients:"
                    match = re.search(r'inactive\s+(ingredients?|components?)[:]\s*(.+)', text, re.I)
                    if match:
                        ingredients_text = match.group(2)
                        parts = re.split(r'[,;•\n\r]+', ingredients_text)
                        for part in parts:
                            part = part.strip()
                            if part and len(part) > 2 and not re.match(r'^\d+$', part):
                                ingredients.append(part.lower())
                        if not verbose_info['strategy_used']:
                            verbose_info['strategy_used'] = 2
                break
        
        # Strategy 3: Look for collapsible/accordion sections
        if not ingredients:
            verbose_info['strategies_tried'].append({'strategy': 3, 'name': 'collapsible_accordion_sections'})
            collapsible_elems = soup.find_all(['div', 'section'], class_=re.compile(r'collapse|expand|accordion|dropdown', re.I))
            for elem in collapsible_elems:
                elem_text = elem.get_text()
                if re.search(r'inactive\s+(ingredients?|components?)', elem_text, re.I):
                    verbose_info['strategy_used'] = 3
                    # Extract from this section
                    parts = re.split(r'[,;•\n\r]+', elem_text)
                    for part in parts:
                        part = part.strip()
                        part = re.sub(r'^.*?inactive.*?:?\s*', '', part, flags=re.I)
                        if part and len(part) > 2 and not re.match(r'^\d+$', part):
                            ingredients.append(part.lower())
        
        # Strategy 4: Look for list items with ingredient-like text
        if not ingredients:
            verbose_info['strategies_tried'].append({'strategy': 4, 'name': 'list_items_with_parent_text'})
            for li in soup.find_all('li'):
                text = li.get_text(strip=True)
                parent_text = ''
                if li.parent:
                    parent_text = li.parent.get_text()
                
                # If parent mentions inactive ingredients
                if re.search(r'inactive\s+(ingredients?|components?)', parent_text, re.I):
                    verbose_info['strategy_used'] = 4
                    if text and len(text) > 2 and not re.match(r'^\d+$', text):
                        ingredients.append(text.lower())
        
        # Clean and deduplicate
        cleaned = []
        for ing in ingredients:
            ing = re.sub(r'[^\w\s-]', '', ing)  # Remove special chars except hyphens
            ing = ing.strip()
            if ing and len(ing) > 2:
                cleaned.append(ing)
        
        final_ingredients = list(set(cleaned))  # Deduplicate
        verbose_info['ingredients_count'] = len(final_ingredients)
        if not verbose_info['strategy_used']:
            verbose_info['strategy_used'] = 'none'
        return final_ingredients, verbose_info
    
    def _check_allergies(self, ingredients: List[str]) -> Tuple[bool, Optional[str]]:
        """Check if any allergens are present in ingredients list. Returns (found, allergen_name)."""
        ingredients_text = ' '.join(ingredients).lower()
        
        for allergen in ALLERGENS:
            if allergen.lower() in ingredients_text:
                return True, allergen
        
        return False, None
    
    def process_medication_page(self, url: str) -> Dict:
        """Process a single medication label page and return detailed result with disqualification info."""
        result = {
            'qualified': False,
            'url': url,
            'disqualification_reason': None,
            'title': None,
            'title_info': {},
            'form_analysis': {},
            'inactive_ndc_warning': {},
            'inactive_ingredients': [],
            'ingredient_info': {},
            'allergen_check': {},
            'page_fetch_status': 'success'
        }
        
        # Fetch page
        response = self._make_request(url)
        if not response:
            result['page_fetch_status'] = 'failed'
            result['disqualification_reason'] = 'page_fetch_failed'
            return result
        
        soup = BeautifulSoup(response.text, 'lxml')
        
        # Step 1: Check for inactive NDC warning
        has_warning, warning_info = self._check_inactive_ndc_warning(soup)
        result['inactive_ndc_warning'] = {
            'detected': has_warning,
            'details': warning_info
        }
        if has_warning:
            result['disqualification_reason'] = 'inactive_ndc_warning'
            return result
        
        # Step 2: Extract title
        title, title_info = self._extract_medication_title(soup)
        result['title'] = title
        result['title_info'] = title_info
        if not title:
            result['disqualification_reason'] = 'title_not_found'
            return result
        
        # Step 3: Analyze form type
        form_analysis = self._analyze_form_type_ai(title)
        result['form_analysis'] = form_analysis
        if form_analysis.get('form_type') in ['disqualify', 'unknown']:
            result['disqualification_reason'] = f"form_type_{form_analysis.get('form_type')}"
            return result
        
        form_type = form_analysis.get('form_type', 'unknown')
        
        # Step 4: Extract inactive ingredients
        inactive_ingredients, ingredient_info = self._extract_inactive_ingredients_ai(soup)
        result['inactive_ingredients'] = inactive_ingredients
        result['ingredient_info'] = ingredient_info
        
        # Step 5: Check for allergens
        has_allergen, allergen_name = self._check_allergies(inactive_ingredients)
        result['allergen_check'] = {
            'has_allergen': has_allergen,
            'allergen_found': allergen_name
        }
        if has_allergen:
            result['disqualification_reason'] = f'allergen_found_{allergen_name}'
            return result
        
        # Qualified!
        result['qualified'] = True
        result['form_type'] = form_type
        return result
    
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
            print(f"[{i}/{total}] Processing: {url}")
            
            result = self.process_medication_page(url)
            
            if result['qualified']:
                qualified_results.append(result)
                print(f"  ✓ QUALIFIED ({result.get('form_type', 'unknown')})")
                print(f"    URL: {result.get('url', url)}")
                if self.verbose:
                    print(f"    Title: {result['title']}")
                    ingredients = result.get('inactive_ingredients', [])
                    print(f"    Ingredients found: {len(ingredients)}")
                    if ingredients:
                        ingredient_info = result.get('ingredient_info', {})
                        method = ingredient_info.get('method', 'unknown')
                        strategy = ingredient_info.get('strategy_used', 'unknown')
                        print(f"      Extraction method: {method}")
                        if strategy and strategy != 'none':
                            print(f"      Strategy used: {strategy}")
                        if len(ingredients) <= 15:
                            print(f"      Ingredients: {', '.join(ingredients)}")
                        else:
                            print(f"      First 15 ingredients: {', '.join(ingredients[:15])}...")
                            print(f"      (Total: {len(ingredients)} ingredients)")
            else:
                if self.verbose:
                    self._print_disqualification_details(result)
                else:
                    print(f"  ✗ Disqualified: {result.get('disqualification_reason', 'unknown')}")
            
            if self.verbose:
                print()  # Extra line for readability
            
            time.sleep(0.1)  # Be respectful
        
        return qualified_results
    
    def _print_disqualification_details(self, result: Dict):
        """Print detailed disqualification information in verbose mode."""
        print(f"  ✗ Disqualified: {result.get('disqualification_reason', 'unknown')}")
        print(f"    Details:")
        
        # Page fetch status
        if result.get('page_fetch_status') == 'failed':
            print(f"      Page fetch: FAILED")
            return
        
        # Title information
        title = result.get('title')
        title_info = result.get('title_info', {})
        if title:
            print(f"      Title: \"{title}\"")
            found_in = title_info.get('found_in')
            if found_in:
                print(f"      Title found in: {found_in}")
            if self.verbose and title_info.get('selectors_tried'):
                print(f"      Title selectors tried:")
                for sel_info in title_info['selectors_tried']:
                    status = "✓ found" if sel_info.get('found') else "✗ not found"
                    print(f"        - {sel_info['selector']}: {status}")
        else:
            print(f"      Title: Not found")
            if title_info.get('selectors_tried'):
                print(f"      Selectors tried: {', '.join([s['selector'] for s in title_info['selectors_tried']])}")
        
        # Inactive NDC warning
        warning_info = result.get('inactive_ndc_warning', {})
        if warning_info.get('detected'):
            print(f"      Inactive NDC Warning: DETECTED")
            details = warning_info.get('details', {})
            detection_method = details.get('detection_method', 'unknown')
            print(f"        Detection method: {detection_method}")
            if details.get('inactive_ndc_tag_found'):
                print(f"        Found inactive-ndc-tag class element(s)")
            if details.get('warning_text_matches'):
                print(f"        Warning text matches: {details['warning_text_matches']}")
            if details.get('red_styled_found'):
                print(f"        Red styling found: Yes")
        else:
            print(f"      Inactive NDC Warning: Not detected")
        
        # Form analysis
        form_analysis = result.get('form_analysis', {})
        if form_analysis:
            form_type = form_analysis.get('form_type', 'unknown')
            confidence = form_analysis.get('confidence', 'unknown')
            reasoning = form_analysis.get('reasoning', '')
            print(f"      Form Type: {form_type} (confidence: {confidence})")
            if reasoning:
                print(f"        Reasoning: {reasoning}")
            if form_type in ['disqualify', 'unknown']:
                return  # Skip ingredient info if disqualified at form stage
        
        # Inactive ingredients
        ingredient_info = result.get('ingredient_info', {})
        ingredients = result.get('inactive_ingredients', [])
        if ingredients:
            print(f"      Inactive ingredients found: {len(ingredients)}")
            method = ingredient_info.get('method', 'unknown')
            strategy = ingredient_info.get('strategy_used', 'unknown')
            print(f"        Extraction method: {method}")
            if strategy and strategy != 'none':
                print(f"        Strategy used: {strategy}")
            if len(ingredients) <= 15:
                print(f"        Ingredients ({len(ingredients)}): {', '.join(ingredients)}")
            else:
                print(f"        Ingredients ({len(ingredients)} total): {', '.join(ingredients[:15])}...")
                print(f"        (Showing first 15 of {len(ingredients)} ingredients)")
        else:
            print(f"      Inactive ingredients: Not found")
            method = ingredient_info.get('method', 'unknown')
            strategies = ingredient_info.get('strategies_tried', [])
            print(f"        Extraction method: {method}")
            if strategies:
                print(f"        Strategies tried: {', '.join([str(s.get('strategy', '')) for s in strategies])}")
        
        # Allergen check
        allergen_check = result.get('allergen_check', {})
        if allergen_check.get('has_allergen'):
            allergen = allergen_check.get('allergen_found', 'unknown')
            print(f"      Allergens: FOUND '{allergen}'")
        else:
            print(f"      Allergens: None detected")
    
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
                output.append(f"{i}. {result.get('title', 'Unknown')}")
                output.append(f"   {result.get('url', '')}")
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
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose output showing detailed disqualification reasons and selector attempts'
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
    searcher = MedicationSearcher(openai_api_key=openai_key, verbose=args.verbose)
    
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

