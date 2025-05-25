import requests
from urllib.parse import quote
from lxml import etree
import os # Added for os.path and os.makedirs

BASE_URL = "https://dailymed.nlm.nih.gov/dailymed/services/v2"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json"
}

NS = {'hl7': 'urn:hl7-org:v3'}
SPL_CACHE_DIR = os.path.join("/tmp", "spl_cache") # Use /tmp/spl_cache for App Engine compatibility

# Ensure the cache directory exists (only if writable)
try:
    if not os.path.exists(SPL_CACHE_DIR):
        os.makedirs(SPL_CACHE_DIR)
except OSError:
    # On read-only file systems, skip cache directory creation
    pass

def search_dailymed_drug(drug_name, max_results=10):
    """
    Search DailyMed for drug labels by generic name and return relevant results.
    Tries both 'search' and 'active_ingredient' parameters. Returns a list of dictionaries with setid, title, and other metadata.
    """
    url_search = f"{BASE_URL}/druglabels.json?search={quote(drug_name)}&pagesize={max_results}"
    try:
        response = requests.get(url_search, headers=HEADERS, timeout=10)
        print(f"Request URL (search): {url_search}")
        print(f"Final URL: {response.url}")
        print(f"Status: {response.status_code}")
        print(f"Content-Type: {response.headers.get('Content-Type')}")
        print(f"Response (first 500 chars): {response.text[:500]}")
        response.raise_for_status()
        data = response.json()
        if data.get("data"):
            return data.get("data", [])
    except Exception as e:
        print(f"Error with 'search' param: {e}")

    # Try the 'active_ingredient' parameter
    url_active = f"{BASE_URL}/druglabels.json?active_ingredient={quote(drug_name)}&pagesize={max_results}"
    try:
        response = requests.get(url_active, headers=HEADERS, timeout=10)
        print(f"Request URL (active_ingredient): {url_active}")
        print(f"Final URL: {response.url}")
        print(f"Status: {response.status_code}")
        print(f"Content-Type: {response.headers.get('Content-Type')}")
        print(f"Response (first 500 chars): {response.text[:500]}")
        response.raise_for_status()
        data = response.json()
        if data.get("data"):
            return data.get("data", [])
    except Exception as e:
        print(f"Error with 'active_ingredient' param: {e}")

    # Try the /drugnames.json endpoint as a fallback
    url_names = f"{BASE_URL}/drugnames.json?name={quote(drug_name)}"
    try:
        response = requests.get(url_names, headers=HEADERS, timeout=10)
        print(f"Request URL (drugnames): {url_names}")
        print(f"Final URL: {response.url}")
        print(f"Status: {response.status_code}")
        print(f"Content-Type: {response.headers.get('Content-Type')}")
        print(f"Response (first 500 chars): {response.text[:500]}")
        response.raise_for_status()
        data = response.json()
        return data
    except Exception as e:
        print(f"Error with 'drugnames' param: {e}")

    return []

def get_dosing_info(setid):
    """
    Retrieve dosing and administration information for a specific SETID.
    Returns the dosage_and_administration field or None if not found.
    """
    url = f"{BASE_URL}/spls/{setid}.json"
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get("dosage_and_administration", ["No dosing information available"])
    except requests.exceptions.RequestException as e:
        print(f"Error retrieving dosing info for SETID {setid}: {e}")
        return None

def list_spls(drug_name, max_results=10):
    url = f"{BASE_URL}/spls.json?drug_name={drug_name}&pagesize={max_results}"
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.json().get("data", [])

def fetch_spl_xml(setid):
    """Fetch SPL XML from DailyMed API, with local caching."""
    cache_file_path = os.path.join(SPL_CACHE_DIR, f"{setid}.xml")

    # Check if the file exists in cache
    if os.path.exists(cache_file_path):
        print(f"[INFO] Using cached SPL XML for SETID: {setid}")
        with open(cache_file_path, 'rb') as f: # Read as bytes
            return f.read()

    # If not in cache, fetch from API
    print(f"[INFO] Fetching SPL XML from API for SETID: {setid}")
    url = f"{BASE_URL}/spls/{setid}.xml"
    try:
        resp = requests.get(url, headers=HEADERS)
        resp.raise_for_status() # Raise an exception for HTTP errors
        xml_content = resp.content

        # Save to cache
        with open(cache_file_path, 'wb') as f: # Write as bytes
            f.write(xml_content)
        
        return xml_content
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Failed to fetch SPL XML for SETID {setid}: {e}")
        return None # Return None or raise an exception as appropriate

def extract_dosing_info(xml_content):
    tree = etree.fromstring(xml_content)
    dosing_sections = []
    # More specific keywords for relevant sections
    keywords = [
        "2 DOSAGE AND ADMINISTRATION",
        "DOSAGE AND ADMINISTRATION",
        "DOSAGE FORMS AND STRENGTHS",
        "OVERDOSAGE",
        "HOW SUPPLIED"
    ]
    for section in tree.xpath('.//hl7:section', namespaces=NS):
        title_element = section.find('hl7:title', namespaces=NS)
        if title_element is not None:
            title = "".join(title_element.itertext()).strip().upper()
            # Check if the title STARTS WITH any of the keywords
            if any(title.startswith(keyword) for keyword in keywords):
                section_text_nodes = section.xpath('.//hl7:text', namespaces=NS)
                section_text = ""
                if section_text_nodes: # Check if hl7:text exists
                    section_text = "".join(section_text_nodes[0].itertext()).strip()
                
                if not section_text: # Fallback if text element is not directly there or empty
                    # More robustly get all text within the section
                    current_section_text_elements = []
                    # Iterate over all child elements of the section to build up the text
                    for elem in section:
                        if elem.tag != title_element.tag: # Avoid reprocessing the title element itself
                            current_section_text_elements.append("".join(elem.itertext()).strip())
                    full_section_text = " ".join(filter(None, current_section_text_elements)).strip() # Join with space, filter empty

                    # Remove the title from the beginning of the text if it's repeated to avoid duplication
                    if full_section_text.upper().startswith(title):
                        section_text = full_section_text[len(title):].strip()
                    else:
                        section_text = full_section_text
                
                # Further refinement: remove title from section_text if it's there
                # This is because the title is already added to the dosing_sections list
                if section_text.upper().startswith(title):
                     section_text = section_text[len(title):].strip()

                if section_text: # Only add if there's actual text content
                    # Prepend the original title (with original casing) to the section text
                    original_title = "".join(title_element.itertext()).strip()
                    dosing_sections.append(f"{original_title}:\n{section_text}")
    return dosing_sections

def print_all_section_titles(xml_content):
    tree = etree.fromstring(xml_content)
    print("\nAll section titles in this SPL:")
    for section in tree.xpath('.//hl7:section', namespaces=NS):
        title = section.findtext('hl7:title', namespaces=NS)
        if title:
            print("-", title)

def print_raw_xml_and_tags(xml_content):
    print("\n--- RAW XML (first 2000 chars) ---")
    print(xml_content[:2000].decode(errors='replace'))
    tree = etree.fromstring(xml_content)
    print("\nRoot tag:", tree.tag)
    print("Root attrib:", tree.attrib)
    tags = set()
    for elem in tree.iter():
        tags.add(elem.tag)
    print("\nAll unique tags in XML:")
    for tag in tags:
        print(tag)

def print_all_section_titles_and_text(xml_content):
    tree = etree.fromstring(xml_content)
    print("\nAll section titles and first 200 chars of text in this SPL:")
    for section in tree.xpath('.//hl7:section', namespaces=NS):
        title = section.findtext('hl7:title', namespaces=NS)
        text = "".join(section.itertext())
        print(f"- {title}: {text[:200]}") 