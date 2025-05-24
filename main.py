from dailymed import list_spls, fetch_spl_xml, extract_dosing_info, print_all_section_titles, print_raw_xml_and_tags, print_all_section_titles_and_text
import io
import sys
import re
import os

def log_output(filename, content):
    with open(filename, 'a', encoding='utf-8') as f:
        f.write(content)
        if not content.endswith('\n'):
            f.write('\n')

def summarize_dosing_from_log(logfile, dosingfile=None):
    # Read the log file and extract dosing/administration info
    with open(logfile, 'r', encoding='utf-8') as f:
        log = f.read()
    # Look for the DOSAGE AND ADMINISTRATION section(s)
    dosing_sections = re.findall(r'--- DOSAGE AND ADMINISTRATION SECTIONS ---([\s\S]+?)(?=---|$)', log)
    summary = ""
    if dosing_sections:
        for section in dosing_sections:
            # Try to extract the most relevant paragraphs
            # Remove XML tags for readability
            text = re.sub(r'<[^>]+>', '', section)
            # Collapse whitespace
            text = re.sub(r'\s+', ' ', text)
            summary += text.strip() + '\n\n'
    else:
        summary = "No dosing and administration information found in log.\n"
    log_output(logfile, "\n--- DOSING AND ADMINISTRATION SUMMARY ---\n" + summary)
    if dosingfile:
        with open(dosingfile, 'w', encoding='utf-8') as f:
            f.write(summary)
    return summary

# Gemini API integration
try:
    from google import genai
except ImportError:
    genai = None

def call_gemini_for_orders(dosing_text, api_key):
    if genai is None:
        print("google-genai is not installed. Please run: pip install google-genai")
        return None
    client = genai.Client(api_key=api_key)
    prompt = (
        "You are a clinical pharmacist AI. Extract medication order sentences from the following dosing and administration text.\n\n"
        "Output format:\n"
        "- Start with a brief explanation of the output format.\n"
        "- If there are general dosing instructions (e.g., titration, initial dosing, dose adjustments), list them as plain text above the order sentences.\n"
        "- For each order sentence, use the format: <dose>, <drug_form>, <route>, <frequency>\n"
        "- If the order is for pediatric or neonatal patients, prepend the order sentence with (PEDS) or (NEO).\n"
        "- For each order sentence, specify the filtering criteria (e.g., 'Use for: adults (age ≥ 18)', 'Use for: pediatric (weight < 40kg)', 'Use for: neonatal (PMA < 44 weeks)').\n"
        "- Do not use markdown or bullet points; use plain text only.\n"
        "- If there are comments or special instructions, add them after the order sentence as a comment.\n\n"
        "Example output:\n"
        "Order sentences are listed below. General instructions are provided first if applicable.\n\n"
        "General instructions:\n"
        "Start with 10 mg daily and increase by 10 mg every 2 weeks as tolerated.\n\n"
        "Order sentences:\n"
        "(PEDS) 10 mg, capsule, oral, daily\n"
        "Use for: pediatric patients (age 6-17 years, weight < 40kg)\n\n"
        "20 mg, capsule, oral, daily\n"
        "Use for: adults (age ≥ 18 years)\n\n"
        "NEO 5 mg, solution, oral, every 12 hours\n"
        "Use for: neonatal patients (PMA < 44 weeks)\n\n"
        "Text:\n"
        + dosing_text
    )
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt
    )
    return response.text

def main():
    drug_name = input("Enter drug name: ").strip()
    spls = list_spls(drug_name, max_results=50)  # Fetch more to allow filtering
    if not spls:
        return

    preferred_categories = {"NDA", "ANDA", "BLA"}
    filtered_spls = [spl for spl in spls if spl.get('marketing_category') in preferred_categories]
    filtered_spls = filtered_spls[:10]

    if filtered_spls:
        print("\nAvailable SPLs (NDA/ANDA/BLA only):")
        display_spls = filtered_spls
    else:
        print("\nNo NDA/ANDA/BLA SPLs found. Showing all SPLs instead:")
        display_spls = spls[:10]

    for idx, spl in enumerate(display_spls, 1):
        cat = spl.get('marketing_category', 'N/A')
        labeler = spl.get('labeler_name', 'N/A')
        star = '*' if cat in preferred_categories else ' '
        print(f"{idx}. {star}{spl['title']} (SETID: {spl['setid']}) - Published: {spl.get('published_date', 'N/A')} - Version: {spl.get('spl_version', 'N/A')} - Category: {cat} - Labeler: {labeler}")

    if not display_spls:
        print("No SPLs found.")
        return

    choice = int(input("\nSelect SPL number to fetch: ")) - 1
    setid = display_spls[choice]['setid']
    logfile = f"spl_{setid}.log"
    dosingfile = f"dosing_{setid}.txt"
    xml_content = fetch_spl_xml(setid)

    # Log raw XML
    log_output(logfile, "\n--- RAW XML (first 2000 chars) ---\n" + xml_content[:2000].decode(errors='replace'))

    # Log root tag and unique tags
    from lxml import etree
    tree = etree.fromstring(xml_content)
    log_output(logfile, f"\nRoot tag: {tree.tag}\nRoot attrib: {tree.attrib}")
    tags = set()
    for elem in tree.iter():
        tags.add(elem.tag)
    log_output(logfile, "\nAll unique tags in XML:\n" + "\n".join(tags))

    # Log all section titles and text
    old_stdout = sys.stdout
    sys.stdout = mystdout = io.StringIO()
    print_all_section_titles(xml_content)
    print_all_section_titles_and_text(xml_content)
    sys.stdout = old_stdout
    log_output(logfile, mystdout.getvalue())

    # Log dosing sections
    dosing_sections = extract_dosing_info(xml_content)
    if dosing_sections:
        log_output(logfile, "\n--- DOSAGE AND ADMINISTRATION SECTIONS ---")
        for section in dosing_sections:
            log_output(logfile, section)
    else:
        log_output(logfile, "No dosing info found.")

    # Summarize dosing and administration info from log and write to separate file
    dosing_summary = summarize_dosing_from_log(logfile, dosingfile)

    # Call Gemini API for LLM-based order sentence extraction
    print("\n--- GEMINI ORDER SENTENCES ---")
    gemini_api_key = "AIzaSyCgYnLijWW-AY3ZS5oBRVbmTmKk-lkpN-c"
    gemini_output = call_gemini_for_orders(dosing_summary, gemini_api_key)
    if gemini_output:
        print(gemini_output)

if __name__ == "__main__":
    main() 