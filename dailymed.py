import requests
from urllib.parse import quote
from lxml import etree

BASE_URL = "https://dailymed.nlm.nih.gov/dailymed/services/v2"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json"
}

NS = {'hl7': 'urn:hl7-org:v3'}

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
    url = f"{BASE_URL}/spls/{setid}.xml"
    resp = requests.get(url, headers=HEADERS)
    resp.raise_for_status()
    return resp.content

def extract_dosing_info(xml_content):
    tree = etree.fromstring(xml_content)
    dosing_sections = []
    for section in tree.xpath('.//hl7:section', namespaces=NS):
        title = section.findtext('hl7:title', namespaces=NS)
        text = "".join(section.itertext())
        if (title and "DOSAGE" in title.upper()) or ("DOSAGE" in text.upper()):
            dosing_sections.append(etree.tostring(section, pretty_print=True, encoding='unicode'))
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