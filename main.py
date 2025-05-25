from flask import Flask, render_template, request, jsonify
from dailymed import list_spls, fetch_spl_xml, extract_dosing_info, print_all_section_titles, print_raw_xml_and_tags, print_all_section_titles_and_text
import io
import sys
import re
import os
import requests
from datetime import datetime
from collections import defaultdict
from dotenv import load_dotenv
import pandas as pd
import markdown # Add this import
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

def log_output(content):
    logging.info(content)

GEMINI_API_KEY_GLOBAL = os.getenv("GEMINI_API_KEY")
log_output(f"[DEBUG] GEMINI_API_KEY from environment: {GEMINI_API_KEY_GLOBAL}")
print(f"[DEBUG] GEMINI_API_KEY from environment: {GEMINI_API_KEY_GLOBAL}")

app = Flask(__name__) # Initialize Flask App

# --- log_output, summarize_dosing_from_log, Gemini functions, RxNorm/DailyMed/openFDA functions remain unchanged ---
# --- They are not directly used by the Flask app in this iteration, but kept for potential future re-integration ---

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
    log_output(f"--- DOSING AND ADMINISTRATION SUMMARY ---\n{summary}")
    if dosingfile:
        with open(dosingfile, 'w', encoding='utf-8') as f:
            f.write(summary)
    return summary

# Gemini API integration
try:
    from google import genai
except ImportError:
    genai = None

# --- Pricing for gemini-2.0-flash (USD per 1 million tokens) ---
# Based on Vertex AI pricing page for Gemini 2.0 Flash (standard, non-batch)
# Input: $0.15 / 1M tokens  => $0.00000015 / token
# Output: $0.60 / 1M tokens => $0.00000060 / token
PRICE_PER_INPUT_TOKEN = 0.00000015
PRICE_PER_OUTPUT_TOKEN = 0.00000060

def get_cerner_order_sentences(drug_name, excel_path, sheet_name, description_col, sentence_col):
    """
    Reads an Excel file, filters by drug name in a description column, 
    and returns corresponding rows as a list of dictionaries.
    """
    try:
        # Debug: Print working directory and file existence
        cwd = os.getcwd()
        file_exists = os.path.exists(excel_path)
        abs_path = os.path.abspath(excel_path)
        files_in_root = os.listdir(cwd)
        log_output(f"[DEBUG] Current working directory: {cwd}")
        log_output(f"[DEBUG] Excel file path checked: {excel_path}, exists: {file_exists}")
        log_output(f"[DEBUG] Absolute path to Excel file: {abs_path}")
        log_output(f"[DEBUG] Files in project root: {files_in_root}")
        print(f"[DEBUG] Current working directory: {cwd}")
        print(f"[DEBUG] Excel file path checked: {excel_path}, exists: {file_exists}")
        print(f"[DEBUG] Absolute path to Excel file: {abs_path}")
        print(f"[DEBUG] Files in project root: {files_in_root}")
        # Specify that the second row (index 1) contains the headers
        df = pd.read_excel(excel_path, sheet_name=sheet_name, header=1)
        
        # Perform a case-insensitive search for the drug_name in the description_col
        # Ensure both drug_name and the column are treated as strings
        mask = df[description_col].astype(str).str.contains(drug_name, case=False, na=False)
        filtered_df = df[mask]
        
        if not filtered_df.empty:
            # Replace np.nan with None for JSON compatibility
            processed_df = filtered_df.where(pd.notnull(filtered_df), None)
            # Convert the processed DataFrame to a list of dictionaries
            rows_as_dicts = processed_df.to_dict(orient='records')
            log_output(f"[INFO] Found {len(rows_as_dicts)} Cerner order entries (all columns) for '{drug_name}' in '{excel_path}' sheet '{sheet_name}'.")
            return rows_as_dicts
        else:
            log_output(f"[INFO] No Cerner order entries found for '{drug_name}' in '{excel_path}' sheet '{sheet_name}'.")
            return []
    except FileNotFoundError:
        log_output(f"[ERROR] Excel file not found: {excel_path}")
        print(f"[ERROR] Excel file not found: {excel_path}")
        return [{"Error": f"Excel file not found: {excel_path}"}] 
    except KeyError as e:
        cols = list(df.columns) if 'df' in locals() and hasattr(df, 'columns') else 'Unknown (file not read or DataFrame not created)'
        log_output(f"[ERROR] Column not found in Excel sheet '{sheet_name}': {e}. Available columns: {cols}")
        print(f"[ERROR] Column not found in Excel sheet '{sheet_name}': {e}. Available columns: {cols}")
        return [{"Error": f"Column {e} not found in sheet '{sheet_name}'. Available columns: {cols}"}]
    except Exception as e:
        log_output(f"[ERROR] Failed to read or process Cerner Excel file '{excel_path}': {str(e)}")
        print(f"[ERROR] An unexpected error occurred while processing the Excel file: {str(e)}")
        return [{"Error": f"Could not process Excel file - {str(e)}"}]

def call_gemini_for_orders(drug_name, api_key, task, dosing_text=None, dosage_form_name=None, per_form_summaries=None, all_forms_list=None):
    if genai is None:
        print("google-genai is not installed. Please run: pip install google-genai")
        return "Error: google-genai library not installed.", 0, 0, 0.0 # text, prompt_tokens, completion_tokens, cost
    
    try:
        client = genai.Client(api_key=api_key)
    except Exception as e:
        log_output(f"[ERROR] Failed to initialize Gemini client: {str(e)}")
        return f"Error: Failed to initialize Gemini client - {str(e)}", 0, 0, 0.0

    prompt = ""
    model_name = "gemini-1.5-flash" # Using the model name as per previous an user request and SDK examples

    if task == 'extract_per_form':
        if not dosing_text or not dosage_form_name:
            return "Error: dosing_text and dosage_form_name are required for extract_per_form task.", 0, 0, 0.0
        prompt = (
            f"You are a clinical pharmacist AI. The following is the full XML content of a Structured Product Label (SPL) for {drug_name} {dosage_form_name.upper()}. "
            f"Analyze this entire XML document to extract all distinct dosing and administration order sentences. "
            f"Present each order sentence as a simple string on a new line in the format: <dose or dose range>, <route>, <frequency>. "
            f"Include context like 'adult', 'pediatric', 'initial dose', or specific indications if mentioned with the regimen. "
            f"Example: 20 mg, oral, daily (Adult, Initial for MDD). "
            f"If no dosing information is present, explicitly state 'No order sentences.'. " # Ensure this instruction is clear
            f"Do not add any other conversational text or explanations, only the order sentences or 'No order sentences.'." # Reinforce output format
            f"\\n\\n--- BEGIN SPL XML --- \\n{dosing_text}\\n--- END SPL XML ---" # XML clearly demarcated
        )
        log_output(f"[DEBUG] Gemini Prompt (extract_per_form) for {drug_name} {dosage_form_name}:\\n{prompt[:1000]}... (prompt truncated for log)\\n") # Ensure full prompt start is logged
    
    elif task == 'consolidate_summaries':
        if not per_form_summaries:
            return "Error: per_form_summaries are required for consolidate_summaries task.", 0, 0, 0.0
        
        formatted_summaries = ""
        for form, summary in per_form_summaries.items():
            formatted_summaries += f"Dosage Form: {form}\\n{summary}\\n\\n"
        
        prompt = (
            f"You are a clinical pharmacist AI. You have been provided with extracted dosing order sentences for various dosage forms of the drug {drug_name}. "
            f"Your task is to synthesize this information into a single, concise, well-organized summary table or structured text. "
            f"The final output should be easy to read and highlight key similarities and differences in dosing across forms (e.g., 'Dosing is similar for CAPSULE, LIQUID, and SOLUTION... however, DELAYED RELEASE PELLETS allow for weekly dosing.'). "
            f"Explicitly state if a dosage form had 'No order sentences.' found. "
            f"Do not repeat every single order sentence if many are similar; instead, summarize the patterns. "
            f"Prioritize clarity and clinical relevance. "
            f"Output format should be Markdown. Start with a high-level summary statement if possible, then list details per dosage form or group similar forms. "
            f"Here is the per-form information:\\n\\n{formatted_summaries}"
        )
        log_output(f"[DEBUG] Gemini Prompt (consolidate_summaries) for {drug_name}:\\n{prompt[:1000]}... (prompt truncated for log)\\n")

    elif task == 'prioritize_dosage_forms':
        if not all_forms_list:
            return "Error: all_forms_list is required for prioritize_dosage_forms task.", 0, 0, 0.0
        
        forms_string = "\\n".join([f"- {form}" for form in all_forms_list])
        prompt = (
            f"You are a clinical pharmacist AI. For the drug {drug_name}, the following dosage forms have been identified:\\n"
            f"{forms_string}\\n\\n"
            f"Review this list and identify a subset of these dosage forms that are most likely to have clinically distinct dosing regimens or administration instructions. "
            f"Consider factors like route of administration (oral, injectable, topical, etc.), release mechanisms (e.g., immediate release, extended release, delayed release), and general formulation type (e.g., tablet, capsule, solution, suspension, cream, ointment, patch, implant). "
            f"For example, 'TABLET' and 'CAPSULE' (both oral immediate release) might have similar dosing, but 'TABLET' and 'INJECTION' or 'TABLET' and 'TABLET, EXTENDED RELEASE' would likely differ. "
            f"Return a comma-separated list of the selected dosage form strings. Only include forms from the provided list. "
            f"Aim to select a concise list that covers the major expected variations in dosing. If all forms are highly similar (e.g., only different strengths of the same oral tablet), you might return just one or two representative forms. If all forms are very distinct, you might return most or all of them."
            f"Output ONLY the comma-separated list of dosage form strings."
        )
        log_output(f"[DEBUG] Gemini Prompt (prioritize_dosage_forms) for {drug_name}:\\n{prompt[:1000]}... (prompt truncated for log)\\n")

    elif task == 'summarize_cerner_data':
        if not dosing_text: # dosing_text will carry the formatted Cerner data string
            return "Error: Formatted Cerner data (dosing_text) is required for summarize_cerner_data task.", 0, 0, 0.0
        prompt = (
            f"You are a clinical pharmacist AI. You have been provided with a list of existing pharmacy order sentences for the drug {drug_name} from a hospital formulary system (Cerner). "
            f"Your task is to synthesize this information into a single, concise, well-organized summary table or structured text in Markdown format. "
            f"The summary should highlight key dosing regimens, strengths, and any notable patterns (e.g., common starting doses, titration schedules if apparent, different routes). "
            f"Do not just list every sentence. Group similar orders or summarize trends. "
            f"Prioritize clarity and clinical relevance for a pharmacist reviewing these model orders. "
            f"Here is the list of Cerner order sentences:\\n\\n{dosing_text}"
        )
        log_output(f"[DEBUG] Gemini Prompt (summarize_cerner_data) for {drug_name}:\\n{prompt[:1000]}... (prompt truncated for log)\\n")

    else:
        return f"Error: Unknown task for call_gemini_for_orders: {task}", 0, 0, 0.0

    prompt_tokens = 0
    completion_tokens = 0
    cost = 0.0

    try:
        # Count prompt tokens before the call (for estimation/logging if needed)
        # Note: The actual billed prompt_tokens might slightly differ, usage_metadata is the source of truth for billing.
        count_response = client.models.count_tokens(model=model_name, contents=[prompt])
        estimated_prompt_tokens = count_response.total_tokens
        log_output(f"[INFO] Gemini ({task} for {drug_name}{f' - {dosage_form_name}' if dosage_form_name else ''}): Estimated prompt tokens: {estimated_prompt_tokens}")

        response = client.models.generate_content(
            model=model_name,
            contents=[prompt]
        )

        response_text = ""
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            response_text = "".join(part.text for part in response.candidates[0].content.parts if hasattr(part, 'text'))
        
        if not response_text and hasattr(response, 'prompt_feedback') and response.prompt_feedback and response.prompt_feedback.block_reason:
            block_reason_msg = f"Blocked due to: {response.prompt_feedback.block_reason.name}"
            if response.prompt_feedback.block_reason_message:
                block_reason_msg += f" ({response.prompt_feedback.block_reason_message})"
            log_output(f"[WARN] Gemini prompt for {drug_name} ({task}) was blocked. {block_reason_msg}. Safety ratings: {response.prompt_feedback.safety_ratings if response.prompt_feedback else 'N/A'}")
            response_text = f"Error: API call blocked. {block_reason_msg}"
        elif not response_text:
            log_output(f"[WARN] Gemini response for {drug_name} ({task}) was empty. Parts: {response.candidates[0].content.parts if response.candidates else 'No candidates'}")
            response_text = "Error: Empty response from API."

        # Extract token usage from metadata for accurate billing count
        if hasattr(response, 'usage_metadata') and response.usage_metadata:
            prompt_tokens = response.usage_metadata.prompt_token_count
            completion_tokens = response.usage_metadata.candidates_token_count
            cost = (prompt_tokens * PRICE_PER_INPUT_TOKEN) + (completion_tokens * PRICE_PER_OUTPUT_TOKEN)
            log_output(f"[INFO] Gemini ({task} for {drug_name}{f' - {dosage_form_name}' if dosage_form_name else ''}): Prompt Tokens: {prompt_tokens}, Completion Tokens: {completion_tokens}, Cost: ${cost:.6f}")
        else:
            log_output(f"[WARN] Gemini ({task} for {drug_name}{f' - {dosage_form_name}' if dosage_form_name else ''}): Usage metadata not found in response. Cannot calculate exact tokens/cost.")
            # Use estimated prompt tokens if actuals are not available, completion tokens unknown.
            prompt_tokens = estimated_prompt_tokens
            cost = prompt_tokens * PRICE_PER_INPUT_TOKEN # Cost only for input if output failed or metadata missing
            response_text += " (Warning: Could not retrieve exact token counts or cost from API response)"

        return response_text, prompt_tokens, completion_tokens, cost

    except Exception as e:
        log_output(f"[ERROR] Exception during Gemini API call ({task} for {drug_name}): {str(e)}")
        # Cost will be based on estimated prompt tokens if the call failed after counting.
        cost = estimated_prompt_tokens * PRICE_PER_INPUT_TOKEN if 'estimated_prompt_tokens' in locals() and estimated_prompt_tokens > 0 else 0.0
        log_output(f"[INFO] Gemini ({task} for {drug_name}{f' - {dosage_form_name}' if dosage_form_name else ''}): Estimated Prompt Tokens (on error): {estimated_prompt_tokens if 'estimated_prompt_tokens' in locals() else 'N/A'}, Cost (on error): ${cost:.6f}")
        return f"Error: Exception during Gemini API call - {str(e)}", (estimated_prompt_tokens if 'estimated_prompt_tokens' in locals() else 0), 0, cost

def get_rxcuis_for_drug(drug_name):
    # Use DailyMed API to get RxCUIs for a drug name
    url = f"https://dailymed.nlm.nih.gov/dailymed/services/v2/rxcuis.json?drug_name={drug_name}"
    resp = requests.get(url)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return data

def get_spls_for_rxcui(rxcui):
    url = f"https://dailymed.nlm.nih.gov/dailymed/services/v2/rxcuis/{rxcui}/spls.json"
    try:
        resp = requests.get(url)
        if resp.status_code == 200:
            try:
                return resp.json().get("data", [])
            except Exception:
                return []
    except Exception:
        pass
    return []

def get_rxnorm_properties(rxcui):
    url = f'https://rxnav.nlm.nih.gov/REST/rxcui/{rxcui}/properties.json'
    try:
        resp = requests.get(url)
        if resp.status_code == 200:
            return resp.json().get('properties', {})
    except Exception:
        pass
    return {}

def get_first_spl_title_for_rxcui(rxcui):
    spls = get_spls_for_rxcui(rxcui)
    if spls:
        return spls[0].get('title', 'N/A')
    return 'N/A'

def get_rxcui_from_rxnorm(drug_name):
    url = f'https://rxnav.nlm.nih.gov/REST/rxcui.json?name={drug_name}'
    resp = requests.get(url)
    if resp.status_code == 200:
        id = resp.json().get('idGroup', {}).get('rxnormId', [])
        if id:
            return id[0]
    return None

def get_related_rxcuis(rxcui):
    url = f'https://rxnav.nlm.nih.gov/REST/rxcui/{rxcui}/related.json?tty=SCD+SBD'
    resp = requests.get(url)
    if resp.status_code == 200:
        concepts = resp.json().get('relatedGroup', {}).get('conceptGroup', [])
        rxcuis = []
        for group in concepts:
            for concept in group.get('conceptProperties', []):
                rxcuis.append(concept['rxcui'])
        return rxcuis
    return []

def get_spls_by_ingredient_name(drug_name):
    url = f"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json?drug_name={drug_name}"
    try:
        resp = requests.get(url)
        if resp.status_code == 200:
            return resp.json().get("data", [])
    except Exception:
        pass
    return []

def get_ndcs_for_spl_setid(setid):
    url = f"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{setid}/ndcs.json"
    try:
        resp = requests.get(url)
        if resp.status_code == 200:
            return resp.json().get("data", [])
    except Exception:
        pass
    return []

def get_openfda_application_info(ndc):
    # openFDA expects 10- or 11-digit NDC, dashes removed
    ndc_clean = ndc.replace('-', '')
    url = f"https://api.fda.gov/drug/ndc.json?search=product_ndc:{ndc_clean}"
    try:
        resp = requests.get(url)
        if resp.status_code == 200:
            results = resp.json().get('results', [])
            if results:
                return {
                    'application_number': results[0].get('application_number', ''),
                    'marketing_category': results[0].get('marketing_category', '')
                }
    except Exception:
        pass
    return {}

def get_openfda_spl_setids_by_ingredient(ingredient):
    """Query openFDA for SPL SETIDs based on generic name."""
    
    # Attempt 1: Direct match on generic_name (often tokenized by search engines like Lucene)
    # The `generic_name` field is listed in openFDA harmonization table for the NDC endpoint.
    # We'll let requests handle URL encoding of the ingredient string.
    search_queries = [
        (f"generic_name:\"{ingredient}\""), # Exact phrase first
        (f"generic_name:{ingredient}"),      # Tokenized match
        (f"openfda.generic_name:\"{ingredient}\""), # Try openfda.generic_name with exact phrase
        (f"openfda.generic_name:{ingredient}"), # Try openfda.generic_name tokenized
        (f"openfda.generic_name:*{ingredient.lower()}*") # Wildcard on openfda.generic_name as a last resort
    ]

    all_spls_found = []
    processed_setids = set()
    
    # Use the /drug/ndc.json endpoint as it contains harmonized fields like spl_set_id and generic_name.
    base_url = 'https://api.fda.gov/drug/ndc.json'

    for i, query_segment in enumerate(search_queries):
        url = f'{base_url}?search={query_segment}&limit=1000'
        log_output(f"[DEBUG] Attempt {i+1}/{len(search_queries)}: Querying openFDA with URL: {url}")
        
        try:
            resp = requests.get(url)
            if resp.status_code == 200:
                try:
                    results = resp.json().get('results', [])
                    log_output(f"  [DEBUG] Found {len(results)} raw results from openFDA for query segment '{query_segment}'.")
                except requests.exceptions.JSONDecodeError as json_e:
                    log_output(f"  [ERROR] JSONDecodeError for query segment '{query_segment}'. Status: {resp.status_code}. Response text: {resp.text[:500]}")
                    log_output(f"  Exception: {json_e}")
                    continue # Try next query

                found_relevant_in_attempt = False
                for r in results:
                    active_ingredients = r.get('active_ingredients', [])
                    base_ingredient_match = False
                    for ai in active_ingredients:
                        if ingredient.lower() in ai.get('name', '').lower():
                            base_ingredient_match = True
                            break
                    
                    if not base_ingredient_match:
                        continue 

                    cat = r.get('marketing_category', '')
                    if cat.upper() in ["NDA", "ANDA", "BLA"]:
                        setids_from_result = r.get('openfda', {}).get('spl_set_id', [])
                        if not isinstance(setids_from_result, list):
                            setids_from_result = [setids_from_result] if setids_from_result else []

                        for setid in setids_from_result:
                            if setid and setid not in processed_setids:
                                all_spls_found.append({
                                    'setid': setid,
                                    'marketing_category': cat,
                                    'application_number': r.get('application_number', ''),
                                    'labeler_name': r.get('labeler_name', ''),
                                    'brand_name': r.get('brand_name', ''),
                                    'generic_name_api_hit': r.get('openfda', {}).get('generic_name', [r.get('generic_name', ingredient)])[0],
                                    'dosage_form': r.get('dosage_form', ''), 
                                    'route': r.get('route', []), 
                                    'product_ndc': r.get('product_ndc', ''),
                                    'active_ingredients': active_ingredients
                                })
                                processed_setids.add(setid)
                                found_relevant_in_attempt = True # Mark that this attempt yielded good data
                
                if found_relevant_in_attempt:
                    log_output(f"  [INFO] Found {len(all_spls_found)} relevant SPLs with query segment '{query_segment}'. Using these results.")
                    break # Stop trying other query formats if this one worked
            
            elif resp.status_code == 404:
                log_output(f"  [INFO] Query segment '{query_segment}' resulted in 404 Not Found.")
            else:
                log_output(f"  [WARN] openFDA query with '{query_segment}' failed with status {resp.status_code}: {resp.text[:200]}")
        
        except requests.exceptions.RequestException as e:
            log_output(f"  [ERROR] Request exception for openFDA query with segment '{query_segment}': {e}")
        except Exception as e:
            log_output(f"  [ERROR] Unexpected error during openFDA query with segment '{query_segment}': {str(e)}")
            if 'resp' in locals() and resp:
                 log_output(f"  Response content that might have caused error: {resp.text[:500]}")

    log_output(f"[INFO] Total unique relevant SPLs (NDA/ANDA/BLA) found from openFDA after all query attempts: {len(all_spls_found)}.")
    return all_spls_found

def select_best_spl(drug_name):
    # Step 1: Get ingredient RxCUI from RxNorm
    ingredient_rxcui = get_rxcui_from_rxnorm(drug_name)
    if not ingredient_rxcui:
        return None, 'No RxNorm RxCUI found for this drug name.'
    # Step 2: Get all SCD/SBD RxCUIs for this ingredient
    rxcuis = get_related_rxcuis(ingredient_rxcui)
    # Step 3: Try openFDA by ingredient name for NDA/ANDA/BLA SPL setids
    openfda_spls = get_openfda_spl_setids_by_ingredient(drug_name)
    openfda_setid_map = {s['setid']: s for s in openfda_spls}
    # Step 4: Get SPLs by ingredient from DailyMed
    spls_by_ingredient = get_spls_by_ingredient_name(drug_name)
    # Step 5: Cross-reference SPLs by setid
    crossref_spls = []
    for spl in spls_by_ingredient:
        setid = spl.get('setid')
        if setid in openfda_setid_map:
            s = openfda_setid_map[setid]
            crossref_spls.append({
                'rxcui': None,
                'name': drug_name,
                'dose_form': s.get('dosage_form', spl.get('dosage_form', '')),
                'strength': spl.get('strength', ''),
                'brand': s.get('brand_name', spl.get('brand_name', '')),
                'spl': spl,
                'application_number': s.get('application_number'),
                'marketing_category': s.get('marketing_category'),
                'labeler_name': s.get('labeler_name', spl.get('labeler_name', '')),
                'route': s.get('route', spl.get('route', '')),
                'product_ndc': s.get('product_ndc', '')
            })
    if crossref_spls:
        all_spl_candidates = crossref_spls
    else:
        all_spl_candidates = [{
            'rxcui': None,
            'name': drug_name,
            'dose_form': spl.get('dosage_form', ''),
            'strength': spl.get('strength', ''),
            'brand': spl.get('brand_name', ''),
            'spl': spl
        } for spl in spls_by_ingredient]
    # Step 6: Prefer NDA over ANDA/BLA, sort by published date descending
    def spl_sort_key(item):
        cat = item.get('marketing_category') or item['spl'].get('marketing_category', '')
        date_str = item.get('published_date', '')
        cat_rank = {'NDA': 0, 'ANDA': 1, 'BLA': 2}.get(cat, 3)
        date_val = 0
        for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
            try:
                date_val = int(datetime.strptime(date_str, fmt).strftime("%Y%m%d"))
                break
            except Exception:
                continue
        return (cat_rank, -date_val)
    all_spl_candidates.sort(key=spl_sort_key)
    if all_spl_candidates:
        return all_spl_candidates[0], None
    return None, 'No SPLs found.'

def get_spl_dosage_form_summary(drug_name):
    """Return a string summarizing all available dosage forms and their SPLs for the given drug."""
    spls = get_spls_by_ingredient_name(drug_name)
    from collections import defaultdict
    groups = defaultdict(list)
    for spl in spls:
        form = (spl.get('dosage_form', '') or spl.get('dose_form', '') or 'Other').strip().upper()
        groups[form].append(spl)
    summary_lines = []
    for form, spl_list in sorted(groups.items()):
        spl_summaries = []
        for spl in spl_list:
            title = spl.get('title', 'N/A')
            setid = spl.get('setid', 'N/A')
            brand = spl.get('brand_name', '')
            published = spl.get('published_date', 'N/A')
            cat = spl.get('marketing_category', 'N/A')
            spl_summaries.append(f"{title} (SETID: {setid}){f' (Brand: {brand})' if brand else ''} - Published: {published} - Category: {cat}")
        summary_lines.append(f"- {form}:\n    " + "\n    ".join(spl_summaries))
    return "\n".join(summary_lines)

def get_spls_by_dosage_form_api(drug_name):
    """Query DailyMed API for all SPLs for a drug, group by dosage form, and return a dict."""
    url = f"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json?drug_name={drug_name}"
    resp = requests.get(url)
    resp.raise_for_status()
    spls = resp.json().get("data", [])
    from collections import defaultdict
    groups = defaultdict(list)
    for spl in spls:
        form = (spl.get('dosage_form', '') or spl.get('dose_form', '') or 'Other').strip().upper()
        groups[form].append(spl)
    summary = {}
    for form, spl_list in groups.items():
        summary[form] = [
            {
                "title": spl.get("title", "N/A"),
                "setid": spl.get("setid", "N/A"),
                "brand": spl.get("brand_name", ""),
                "published": spl.get("published_date", "N/A"),
                "category": spl.get("marketing_category", "N/A"),
            }
            for spl in spl_list
        ]
    return summary

def get_best_spl_per_dosage_form(drug_name):
    """Return a dict mapping dosage form to the best SPL for that form (NDA > ANDA > BLA, most recent), using openFDA for NDA/ANDA/BLA filtering. Fallback to all DailyMed SPLs if none found."""
    spls_from_dailymed = get_spls_by_ingredient_name(drug_name)
    openfda_spl_setid_map = {s['setid']: s for s in get_openfda_spl_setids_by_ingredient(drug_name)}
    
    from collections import defaultdict
    from datetime import datetime
    
    # groups will store NDA/ANDA/BLA SPLs, keyed by their openFDA dosage form
    groups = defaultdict(list)
    # fallback_groups will store all DailyMed SPLs, keyed by their DailyMed dosage form
    fallback_groups = defaultdict(list)

    with open(LOG_FILE, 'a', encoding='utf-8') as tlog:
        tlog.write(f"[DEBUG] Total DailyMed SPLs found for {drug_name}: {len(spls_from_dailymed)}\n")
        tlog.write(f"[DEBUG] Total openFDA SPLs (NDA/ANDA/BLA) mapped for {drug_name}: {len(openfda_spl_setid_map)}\n")

    for spl_dm in spls_from_dailymed:
        setid = spl_dm.get('setid')
        # Determine dosage form from DailyMed SPL for fallback grouping
        dm_form_key = (spl_dm.get('dosage_form', '') or spl_dm.get('dose_form', '') or 'Other').strip().upper()
        fallback_groups[dm_form_key].append(spl_dm)

        if setid in openfda_spl_setid_map:
            spl_fda = openfda_spl_setid_map[setid] # Corresponding openFDA data
            marketing_cat = spl_fda.get('marketing_category', '').upper()
            
            if marketing_cat in ('NDA', 'ANDA', 'BLA'):
                # Determine dosage form from openFDA data for primary grouping
                fda_form_key = (spl_fda.get('dosage_form', '') or spl_fda.get('dose_form', '') or 'Other').strip().upper()
                
                # Merge DailyMed SPL data with openFDA SPL data
                # openFDA data takes precedence for shared fields like 'dosage_form', 'marketing_category'
                merged_spl_data = dict(spl_dm)
                merged_spl_data.update(spl_fda)
                
                groups[fda_form_key].append(merged_spl_data)

    # Define sort key for SPLs (used for both groups and fallback_groups)
    def spl_sort_key(spl_item):
        # Ensure 'marketing_category' and 'published_date' are at the top level of spl_item
        cat = spl_item.get('marketing_category', '') 
        date_str = spl_item.get('published_date', '')
        
        cat_rank = {'NDA': 0, 'ANDA': 1, 'BLA': 2}.get(cat.upper(), 3)
        date_val = 0
        for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%Y%m%d"): # Added %Y%m%d for some openFDA dates
            try:
                date_val = int(datetime.strptime(date_str, fmt).strftime("%Y%m%d"))
                break
            except (ValueError, TypeError):
                continue
        return (cat_rank, -date_val)

    best_spls = {}
    used_primary_setids = set() # Keep track of setids used for primary forms

    # Consider all unique dosage form keys found from both openFDA and DailyMed
    all_identified_form_keys = set(groups.keys()).union(fallback_groups.keys())

    with open(LOG_FILE, 'a', encoding='utf-8') as tlog:
        tlog.write(f"[DEBUG] All unique dosage form keys initially identified: {sorted(list(all_identified_form_keys))}\n") # Log all identified keys
        tlog.write(f"[DEBUG] Identifying best SPL for unique form keys: {sorted(list(all_identified_form_keys))}\n")
        
        # First, process and select best SPLs for primary (openFDA-defined) forms
        for form_key in sorted(list(groups.keys())):
            tlog.write(f"[DEBUG] Processing primary form key: {form_key}\n")
            if groups[form_key]:
                candidate_spls = groups[form_key]
                candidate_spls.sort(key=spl_sort_key)
                selected_spl = candidate_spls[0]
                best_spls[form_key] = selected_spl
                used_primary_setids.add(selected_spl['setid'])
                tlog.write(f"  [INFO] Using {len(candidate_spls)} SPL(s) from openFDA mapping for form '{form_key}'.n")
                for s_idx, s_val in enumerate(candidate_spls):
                    tlog.write(f"    - {'*' if s_idx == 0 else ''}{s_val.get('title', 'N/A')} (SETID: {s_val.get('setid')}, Cat: {s_val.get('marketing_category', 'N/A')}, Date: {s_val.get('published_date', 'N/A')})n")
                tlog.write(f"  [SUCCESS] Selected best SPL for primary form '{form_key}': {selected_spl.get('title')} (SETID: {selected_spl.get('setid')})n")
            else:
                tlog.write(f"  [INFO] No openFDA SPLs found for primary form key '{form_key}'.n")

        # Next, process fallback forms, ensuring not to reuse SETIDs already picked for primary forms
        for form_key in sorted(list(fallback_groups.keys())):
            if form_key in best_spls: # Already processed as a primary form
                continue

            tlog.write(f"[DEBUG] Processing fallback form key: {form_key}\n")
            if fallback_groups[form_key]:
                # Filter out SPLs already used for primary forms
                candidate_spls = [s for s in fallback_groups[form_key] if s.get('setid') not in used_primary_setids]
                
                tlog.write(f"  [INFO] Considering {len(fallback_groups[form_key])} DailyMed SPL(s) for fallback form '{form_key}'. Filtered to {len(candidate_spls)} after removing already used SETIDs.n")

                # Additional filter for 'OTHER' fallback to deprioritize injectables
                if form_key == 'OTHER' and candidate_spls:
                    original_other_candidates_count = len(candidate_spls)
                    candidate_spls = [
                        s for s in candidate_spls 
                        if not (
                            'INJECT' in s.get('title','').upper() or 
                            'INJECT' in s.get('dosage_form','').upper() or
                            'INJECT' in s.get('dose_form','').upper()
                        )
                    ]
                    tlog.write(f"    [INFO] For 'OTHER' fallback, further filtered from {original_other_candidates_count} to {len(candidate_spls)} by excluding 'INJECT' keyword in title/form.n")

                if not candidate_spls:
                    tlog.write(f"  [WARN] No suitable fallback SPLs found for form '{form_key}' after filtering used SETIDs and/or 'INJECT' keyword.n")
                    continue

                # Ensure 'marketing_category' is available for sorting fallbacks
                for s in candidate_spls:
                    if 'marketing_category' not in s and s.get('setid') in openfda_spl_setid_map:
                        s['marketing_category'] = openfda_spl_setid_map[s.get('setid')].get('marketing_category', '')
                
                candidate_spls.sort(key=spl_sort_key)
                selected_spl = candidate_spls[0]
                best_spls[form_key] = selected_spl
                # Note: We don't add to used_primary_setids here, as these are fallbacks.
                # However, the check against used_primary_setids prevents a fallback from taking an already chosen primary.
                
                source_info = f"from 'fallback_groups' (DailyMed {len(candidate_spls)} SPLs)"
                tlog.write(f"    (Original fallback count for '{form_key}': {len(fallback_groups[form_key])})n")
                for s_idx, s_val in enumerate(candidate_spls):
                     tlog.write(f"    - {'*' if s_idx == 0 else ''}{s_val.get('title', 'N/A')} (SETID: {s_val.get('setid')}, Cat: {s_val.get('marketing_category', 'N/A')}, Date: {s_val.get('published_date', 'N/A')}) (fallback)n")
                tlog.write(f"  [SUCCESS] Selected best SPL for fallback form '{form_key}' ({source_info}): {selected_spl.get('title')} (SETID: {selected_spl.get('setid')})n")
            else:
                tlog.write(f"  [WARN] No SPLs found in fallback_groups for form key '{form_key}'.n")
                
    if not best_spls:
        with open(LOG_FILE, 'a', encoding='utf-8') as tlog:
            tlog.write(f"[DEBUG] No best SPLs could be determined for any dosage form of {drug_name}.n")

    return best_spls

def format_best_spls_for_gemini(best_spls):
    """Format the best SPLs per dosage form for Gemini context."""
    lines = ["Available dosage forms and best SPLs:"]
    for form, spl in best_spls.items():
        title = spl.get('title', 'N/A')
        setid = spl.get('setid', 'N/A')
        brand = spl.get('brand_name', '')
        published = spl.get('published_date', 'N/A')
        cat = spl.get('marketing_category', 'N/A')
        lines.append(f"- {form}: {title} (SETID: {setid}){f' (Brand: {brand})' if brand else ''} - Published: {published} - Category: {cat}")
    return "\n".join(lines)

def process_all_best_spls(drug_name, gemini_api_key, indication=None):
    """For each best SPL per dosage form, fetch XML, extract dosing info, and call Gemini."""
    best_spls = get_best_spl_per_dosage_form(drug_name)
    results = {}
    for form, spl in best_spls.items():
        setid = spl['setid']
        try:
            xml_content = fetch_spl_xml(setid)
            dosing_sections = extract_dosing_info(xml_content)
            dosing_text = '\n'.join(dosing_sections)
            gemini_context = format_best_spls_for_gemini({form: spl}) + '\n\n'
            gemini_output, prompt_tokens, completion_tokens, cost = call_gemini_for_orders(drug_name, gemini_api_key, 'extract_per_form', dosing_text, form, None)
            results[form] = {
                'spl': spl,
                'gemini_output': gemini_output,
                'dosing_text': dosing_text,
                'prompt_tokens': prompt_tokens,
                'completion_tokens': completion_tokens,
                'cost': cost
            }
        except Exception as e:
            results[form] = {
                'spl': spl,
                'gemini_output': f'Error: {e}',
                'dosing_text': '',
                'prompt_tokens': 0,
                'completion_tokens': 0,
                'cost': 0.0
            }
    return results

def extract_dosing_text_per_form(drug_name, target_dosage_forms_list=None):
    best_spls_by_dosage_form = get_best_spl_per_dosage_form(drug_name)
    if not best_spls_by_dosage_form:
        print(f"[DEBUG] No best SPLs found for {drug_name}")
        log_output(f"[DEBUG] No best SPLs found for {drug_name} in extract_dosing_text_per_form")
        return {}

    compiled_texts_by_form = {} 
    processed_setids = set() 

    forms_to_process = best_spls_by_dosage_form.items()
    if target_dosage_forms_list is not None:
        log_output(f"[INFO] Extracting dosing text only for Gemini-prioritized forms: {target_dosage_forms_list}")
        # Filter forms_to_process based on target_dosage_forms_list
        # Ensure case-insensitive matching for safety, though our prioritized list should match casing.
        target_forms_upper = [f.upper() for f in target_dosage_forms_list]
        forms_to_process = [
            (df, spl) for df, spl in best_spls_by_dosage_form.items() if df.upper() in target_forms_upper
        ]
        if not forms_to_process:
            log_output(f"[WARN] None of the prioritized forms were found in the best_spls_by_dosage_form map for {drug_name}. This is unexpected.")
            # Potentially log best_spls_by_dosage_form.keys() and target_dosage_forms_list for debugging
            return {} # Or decide to process all if this happens

    for dosage_form, spl_data in forms_to_process:
        log_output(f"[DEBUG] Preparing full XML for {drug_name} - Form: {dosage_form}")
        
        setid = spl_data['setid']
        title = spl_data['title']
        
        if setid in processed_setids:
            log_output(f"  Skipping SETID {setid} for form {dosage_form} as it was already processed.")
            continue 

        print(f"[INFO] Preparing full XML for {dosage_form} from: {title} (SETID: {setid})")

        xml_bytes = fetch_spl_xml(setid) # This returns bytes
        if xml_bytes:
            try:
                xml_string = xml_bytes.decode('utf-8') # Decode to string
                compiled_texts_by_form[dosage_form] = xml_string
                log_output(f"  Full XML for {title} (SETID: {setid}) prepared, length {len(xml_string)} chars.")
            except UnicodeDecodeError as e:
                log_output(f"  [ERROR] UnicodeDecodeError for SETID {setid}: {e}. Trying with 'latin-1'.")
                try:
                    xml_string = xml_bytes.decode('latin-1')
                    compiled_texts_by_form[dosage_form] = xml_string
                    log_output(f"  Full XML for {title} (SETID: {setid}) prepared with 'latin-1', length {len(xml_string)} chars.")
                except UnicodeDecodeError as e2:
                    log_output(f"  [ERROR] Failed to decode XML for SETID {setid} with 'latin-1' as well: {e2}")
                    continue # Skip this SPL if decoding fails
            processed_setids.add(setid)
        else:
            log_output(f"  Failed to fetch XML for SETID {setid} for {title}")

    if not compiled_texts_by_form:
        log_output(f"[DEBUG] No XML content compiled from any SPLs for {drug_name}.")
        return {}

    log_output(f"[DEBUG] Finished preparing full XML for {len(compiled_texts_by_form)} forms of {drug_name}.")
    return compiled_texts_by_form

def get_gemini_order_sentences_table(drug_name, gemini_api_key, prioritized_forms_list=None):
    # Step 1: Extract dosing text (full XML) for each (prioritized) dosage form
    # Pass the prioritized_forms_list to extract_dosing_text_per_form
    dosing_texts_by_form = extract_dosing_text_per_form(drug_name, prioritized_forms_list) 

    if not dosing_texts_by_form:
        # If prioritized_forms_list was provided and resulted in no texts, this message is appropriate.
        # If no forms were prioritized, extract_dosing_text_per_form would have used all.
        specific_message = f" (target forms: {prioritized_forms_list})" if prioritized_forms_list else ""
        log_output(f"No dosing texts found for {drug_name}{specific_message} by get_gemini_order_sentences_table. Aborting.")
        return f"No dosing information could be extracted for the targeted dosage forms{specific_message} to summarize.", 0, 0, 0, 0.0

    per_form_summaries = {}
    log_output(f"\n[INFO] Starting per-form Gemini summarization for {drug_name}...")

    total_api_calls = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cost = 0.0

    # Step 2: Call Gemini for each dosage form to get its specific order sentences
    for form_name, specific_dosing_text in dosing_texts_by_form.items():
        log_output(f"  Processing form: {form_name} for per-form Gemini summary.")
        if not specific_dosing_text or specific_dosing_text.strip().startswith("Error:"):
            log_output(f"    Skipping Gemini for {form_name} due to missing or error in dosing text: {specific_dosing_text[:100]}")
            per_form_summaries[form_name] = [specific_dosing_text.strip()] if specific_dosing_text else ["Error: No text to process."]
            continue

        summary_lines, pt, ct, cost = call_gemini_for_orders(
            drug_name=drug_name, 
            api_key=gemini_api_key, 
            task='extract_per_form', 
            dosing_text=specific_dosing_text, 
            dosage_form_name=form_name
        )
        total_api_calls += 1
        total_prompt_tokens += pt
        total_completion_tokens += ct
        total_cost += cost
        
        if summary_lines.strip().startswith("Error:"):
            per_form_summaries[form_name] = [summary_lines.strip()] # Store error message as a list
        else:
            # Split into lines and remove empty ones
            lines = [line.strip() for line in summary_lines.splitlines() if line.strip()]
            if not lines or (len(lines) == 1 and lines[0].lower() == 'no order sentences found.'):
                 per_form_summaries[form_name] = ["No order sentences found."] # Store as a list
            else:
                per_form_summaries[form_name] = lines # Store the list of order sentences
        log_output(f"  Summary for {form_name}:\n{per_form_summaries[form_name]}")

    if not per_form_summaries:
        log_output("No per-form summaries were generated. Aborting final consolidation.")
        return "No per-form summaries generated.", total_api_calls, total_prompt_tokens, total_completion_tokens, total_cost

    log_output(f"\n[INFO] Starting final consolidation Gemini call for {drug_name}...")
    # Step 3: Call Gemini one last time to consolidate all per-form summaries
    final_consolidated_summary, pt, ct, cost = call_gemini_for_orders(
        drug_name=drug_name, 
        api_key=gemini_api_key, 
        task='consolidate_summaries', 
        per_form_summaries=per_form_summaries
    )
    total_api_calls += 1
    total_prompt_tokens += pt
    total_completion_tokens += ct
    total_cost += cost

    log_output(f"\n--- FINAL CONSOLIDATED SUMMARY FOR {drug_name} ---\n{final_consolidated_summary}")
    log_output(f"Summary of Gemini Calls for {drug_name}:")
    log_output(f"  Total API Calls: {total_api_calls}")
    log_output(f"  Total Prompt Tokens: {total_prompt_tokens}")
    log_output(f"  Total Completion Tokens: {total_completion_tokens}")
    log_output(f"  Total Estimated Cost: ${total_cost:.6f}")

    # Return per_form_summaries as well
    return {
        "per_form_orders": per_form_summaries, # Renaming to match template variable
        "consolidated_summary": final_consolidated_summary, # Return raw Markdown
        "total_api_calls": total_api_calls,
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "total_cost": total_cost,
        "error": None # Explicitly no error if we reach here
    }

def select_forms_for_dosing_review(drug_name, all_forms, gemini_api_key):
    """
    Uses Gemini to select a subset of dosage forms most likely to have distinct dosing.
    Returns a list of prioritized dosage form strings.
    """
    if not all_forms:
        log_output(f"[INFO] No dosage forms provided to select_forms_for_dosing_review for {drug_name}. Returning empty list.")
        return []

    log_output(f"[INFO] Asking Gemini to select prioritized dosage forms for {drug_name} from: {all_forms}")

    response_text, pt, ct, cost = call_gemini_for_orders(
        drug_name=drug_name,
        api_key=gemini_api_key,
        task='prioritize_dosage_forms',
        all_forms_list=all_forms
    )
    # TODO: Add cost and token tracking for this new call if needed later, similar to get_gemini_order_sentences_table

    if response_text.startswith("Error:") or not response_text.strip():
        log_output(f"[WARN] Gemini failed to prioritize dosage forms or returned empty: {response_text}. Falling back to using all forms.")
        return all_forms # Fallback to all forms if prioritization fails

    # Gemini is expected to return a comma-separated list
    prioritized_forms = [form.strip().upper() for form in response_text.split(',') if form.strip()]
    
    # Validate that Gemini returned forms that were in the original list (and in uppercase)
    valid_prioritized_forms = []
    all_forms_upper = [f.upper() for f in all_forms]
    for p_form in prioritized_forms:
        if p_form in all_forms_upper:
            # Find the original casing from all_forms to maintain consistency
            original_casing_form = next((orig_f for orig_f in all_forms if orig_f.upper() == p_form), p_form)
            valid_prioritized_forms.append(original_casing_form)
        else:
            log_output(f"[WARN] Gemini suggested a form '{p_form}' not in the original list for {drug_name}. Ignoring it.")
            
    if not valid_prioritized_forms: # If validation removed all forms (e.g. Gemini hallucinated)
        log_output(f"[WARN] Gemini prioritization resulted in no valid forms for {drug_name}. Falling back to using all forms.")
        return all_forms

    log_output(f"[INFO] Gemini prioritized forms for {drug_name}: {valid_prioritized_forms}")
    return valid_prioritized_forms

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/search', methods=['POST'])
def search():
    drug_name = request.form.get('drug_name', '').strip()
    log_output(f"Web app search initiated for drug: {drug_name}")

    excel_path = "Inpatient_Pharmacy_Reference_Build.xlsx"
    sheet_name = "Order Sentences"
    description_column = "Description"
    # sentence_column is implicitly handled by get_cerner_order_sentences returning all columns

    results = get_cerner_order_sentences(
        drug_name=drug_name,
        excel_path=excel_path,
        sheet_name=sheet_name,
        description_col=description_column,
        sentence_col="Sentence" # Still pass a value, though function returns all cols
    )

    error_message = None
    actual_results = []
    # gemini_cerner_summary_html = "" # No longer generated directly here

    if results and isinstance(results[0], dict) and "Error" in results[0]:
        error_message = results[0]["Error"]
        log_output(f"Error during web search for '{drug_name}': {error_message}")
    elif not results: 
        log_output(f"No results found for web search: '{drug_name}'.")
    else:
        actual_results = results
        log_output(f"Successfully found {len(actual_results)} entries for web search: '{drug_name}'.")

        # Gemini summary is now handled by a separate AJAX call triggered from the client-side

    return render_template('results.html', 
                           drug_name=drug_name, 
                           results=actual_results, # Pass the raw results for JS to use
                           error=error_message 
                           # gemini_summary is no longer passed from here
                           )

@app.route('/full_drug_search', methods=['POST'])
def full_drug_search():
    # This route now expects 'drug_name' and 'selected_form_names' (a list of strings)
    drug_name = request.form.get('drug_name', '').strip()
    selected_form_names_str = request.form.get('selected_form_names', '') # Comes as a string with custom delimiter
    
    log_output(f"Full drug information web search (Step 2) initiated for: {drug_name} with selected forms string: {selected_form_names_str}")

    selected_form_names = [name.strip() for name in selected_form_names_str.split('_DSF_DELIM_') if name.strip()] # <-- CHANGED DELIMITER
    log_output(f"Parsed selected forms: {selected_form_names}")

    global GEMINI_API_KEY_GLOBAL
    GEMINI_API_KEY_GLOBAL = os.getenv("GEMINI_API_KEY")
    if not GEMINI_API_KEY_GLOBAL:
        log_output("[ERROR] /full_drug_search: Gemini API Key not found.")
        return render_template('full_results.html', 
                               drug_name=drug_name, 
                               error_message="Critical Error: Gemini API Key not configured.")

    error_msg = None
    per_form_orders_result = {}
    consolidated_summary_html = ""
    # These will be populated to show what was *initially* found vs. what was *selected* for processing
    all_spls_initially_identified_for_display = [] # We might need to fetch these again or pass them through
    processed_spls_for_display = []

    if not drug_name:
        error_msg = "Drug name cannot be empty."
    elif not selected_form_names:
        error_msg = "No dosage forms were selected for processing."
    
    if error_msg:
        log_output(f"[ERROR] Error in /full_drug_search for '{drug_name}': {error_msg}")
        return render_template('full_results.html', 
                               drug_name=drug_name, 
                               error_message=error_msg,
                               prioritized_spls=[], # Keep variable for template
                               non_prioritized_spls=[], # Keep variable for template
                               per_form_orders={},
                               consolidated_summary="")

    try:
        log_output(f"\n--- STARTING FULL DRUG WORKFLOW (STEP 2) FOR: {drug_name} ---")
        log_output(f"  User selected forms for processing: {selected_form_names}")

        # Step 1 (Re-fetch for display consistency): Get all available dosage forms and their best SPLs 
        # This is to ensure full_results.html can show what was *available* vs what was *chosen*.
        # In a more complex app, this data might be passed through from Step 1 via client or session.
        best_spls_map_initial = get_best_spl_per_dosage_form(drug_name)
        for form_name, spl_data in best_spls_map_initial.items():
            spl_item_details = {
                'form_name': form_name,
                'title': spl_data.get('title', 'N/A'),
                'setid': spl_data.get('setid', 'N/A'),
                'published_date': spl_data.get('published_date', 'N/A'),
                'marketing_category': spl_data.get('marketing_category', 'N/A'),
                'pdf_link': f"https://dailymed.nlm.nih.gov/dailymed/spl.cfm?setid={spl_data.get('setid')}&type=pdf" if spl_data.get('setid') else '#'
            }
            all_spls_initially_identified_for_display.append(spl_item_details)
            # Check if this SPL was one of the ones selected by the user for processing
            if form_name in selected_form_names:
                processed_spls_for_display.append(spl_item_details)
        
        # Step 2: Get the consolidated summary and per-form details using ONLY the user-selected form names
        results_data = get_gemini_order_sentences_table(drug_name, GEMINI_API_KEY_GLOBAL, selected_form_names)
        
        per_form_orders_result = results_data.get("per_form_orders", {})
        consolidated_summary_result_md = results_data.get("consolidated_summary", "") # Markdown from function
        error_msg = results_data.get("error")

        if not error_msg:
            log_output(f"[INFO] Full drug workflow (Step 2) for {drug_name} completed.")
            log_output(f"  Total API Calls: {results_data.get('total_api_calls', 'N/A')}")
            log_output(f"  Total Prompt Tokens: {results_data.get('total_prompt_tokens', 'N/A')}")
            log_output(f"  Total Completion Tokens: {results_data.get('total_completion_tokens', 'N/A')}")
            log_output(f"  Total Estimated Cost: ${results_data.get('total_cost', 0.0):.6f}")
        
        if not per_form_orders_result and not consolidated_summary_result_md and not error_msg:
            error_msg = f"No dosing information or summary could be generated for '{drug_name}' based on the selected forms."

        # Convert consolidated_summary_result_md from Markdown to HTML if it exists
        if consolidated_summary_result_md and not error_msg:
            try:
                consolidated_summary_html = markdown.markdown(consolidated_summary_result_md, extensions=['markdown.extensions.tables'])
            except Exception as md_e:
                log_output(f"[ERROR] Failed to convert Gemini summary to HTML: {str(md_e)}")
                consolidated_summary_html = f"<p class='error'>Error rendering summary as HTML: {str(md_e)}</p><pre>{consolidated_summary_result_md}</pre>"
        elif error_msg:
            consolidated_summary_html = ""
        else:
            consolidated_summary_html = consolidated_summary_result_md # Should be empty string if no summary

    except Exception as e:
        error_msg = f"An unexpected critical error occurred during the full drug search (Step 2) for '{drug_name}': {str(e)}"
        log_output(f"[CRITICAL] Exception in /full_drug_search (Step 2) for '{drug_name}': {error_msg}")
        import traceback
        log_output(traceback.format_exc())
        consolidated_summary_html = "" # Ensure it's empty on critical error

    # For full_results.html, `prioritized_spls` will now be the SPLs the user *selected* for processing.
    # `non_prioritized_spls` will be the other SPLs that were initially identified but *not selected* by the user.
    non_processed_spls_for_display = [spl for spl in all_spls_initially_identified_for_display if spl not in processed_spls_for_display]

    return render_template('full_results.html',
                           drug_name=drug_name,
                           prioritized_spls=processed_spls_for_display, # Renamed for clarity in template logic
                           non_prioritized_spls=non_processed_spls_for_display, # Other SPLs not chosen
                           per_form_orders=per_form_orders_result,
                           consolidated_summary=consolidated_summary_html,
                           error_message=error_msg)

@app.route('/summarize_cerner_ajax', methods=['POST'])
def summarize_cerner_ajax():
    data = request.get_json()
    drug_name = data.get('drug_name')
    cerner_results = data.get('cerner_data') # This is a list of dicts

    log_output(f"[INFO] AJAX request received to summarize Cerner data for: {drug_name}")

    if not drug_name or not cerner_results:
        log_output(f"[ERROR] AJAX summarize_cerner_ajax: Missing drug_name or cerner_data.")
        return jsonify(error="Missing drug_name or Cerner data."), 400

    # Format the Cerner data for Gemini (similar to before)
    cerner_data_for_gemini = []
    for row_dict in cerner_results:
        desc = row_dict.get('Description', 'N/A')
        synonym = row_dict.get('Synonym', 'N/A')
        sentence = row_dict.get('Sentence', 'N/A')
        oef = row_dict.get('Order Entry Format', 'N/A')
        cerner_data_for_gemini.append(f"- Description: {desc} | Synonym: {synonym} | Order Entry Format: {oef} | Sentence: {sentence}")
    formatted_cerner_text = "\n".join(cerner_data_for_gemini)

    global GEMINI_API_KEY_GLOBAL
    if not GEMINI_API_KEY_GLOBAL:
        GEMINI_API_KEY_GLOBAL = os.getenv("GEMINI_API_KEY")
    
    if not GEMINI_API_KEY_GLOBAL:
        log_output("[WARN] Gemini API Key not found for AJAX Cerner summary.")
        return jsonify(error="Could not generate summary: Gemini API Key not configured.")

    summary_md, _, _, _ = call_gemini_for_orders(
        drug_name=drug_name,
        api_key=GEMINI_API_KEY_GLOBAL,
        task='summarize_cerner_data',
        dosing_text=formatted_cerner_text
    )

    if summary_md.startswith("Error:"):
        log_output(f"[ERROR] AJAX Gemini failed to summarize Cerner data for {drug_name}: {summary_md}")
        return jsonify(error=summary_md)
    else:
        try:
            summary_html = markdown.markdown(summary_md, extensions=['markdown.extensions.tables'])
            log_output(f"[INFO] AJAX Successfully generated and converted Cerner data summary for {drug_name}.")
            return jsonify(summary_html=summary_html)
        except Exception as md_e:
            log_output(f"[ERROR] AJAX Failed to convert Cerner summary Markdown to HTML: {str(md_e)}")
            # Return raw markdown with an error if conversion fails, or just an error
            return jsonify(summary_html=f"<p class='error'>Error rendering summary: {str(md_e)}</p><pre>{summary_md}</pre>")

@app.route('/fetch_spl_suggestions', methods=['POST'])
def fetch_spl_suggestions():
    data = request.get_json()
    drug_name = data.get('drug_name', '').strip()
    log_output(f"AJAX request received to fetch SPL suggestions for: {drug_name}")

    global GEMINI_API_KEY_GLOBAL
    if not GEMINI_API_KEY_GLOBAL:
        GEMINI_API_KEY_GLOBAL = os.getenv("GEMINI_API_KEY")
    
    if not GEMINI_API_KEY_GLOBAL:
        log_output("[ERROR] Gemini API Key not found for /fetch_spl_suggestions.")
        return jsonify(error="Critical Error: Gemini API Key not configured."), 500

    if not drug_name:
        log_output("[ERROR] /fetch_spl_suggestions: Drug name cannot be empty.")
        return jsonify(error="Drug name cannot be empty."), 400

    try:
        best_spls_map = get_best_spl_per_dosage_form(drug_name)
        if not best_spls_map:
            log_output(f"[INFO] No SPLs found for '{drug_name}' by get_best_spl_per_dosage_form in /fetch_spl_suggestions.")
            return jsonify(all_spls=[], prioritized_form_names=[], error=f"No SPLs found for '{drug_name}'.")

        all_forms_identified_with_spl_data = []
        for form_name, spl_data in best_spls_map.items():
            all_forms_identified_with_spl_data.append({
                'form_name': form_name,
                'title': spl_data.get('title', 'N/A'),
                'setid': spl_data.get('setid', 'N/A'),
                'published_date': spl_data.get('published_date', 'N/A'),
                'marketing_category': spl_data.get('marketing_category', 'N/A'),
                'pdf_link': f"https://dailymed.nlm.nih.gov/dailymed/spl.cfm?setid={spl_data.get('setid')}&type=pdf" if spl_data.get('setid') else '#'
            })
        
        all_form_names_identified = [item['form_name'] for item in all_forms_identified_with_spl_data]
        log_output(f"[INFO] /fetch_spl_suggestions: All dosage forms identified for {drug_name}: {all_form_names_identified}")

        if not all_form_names_identified:
            # This case should ideally be caught by 'if not best_spls_map' earlier, but as a safeguard:
            return jsonify(all_spls=[], prioritized_form_names=[], error=f"No processable dosage forms found for '{drug_name}'.")

        prioritized_form_names = select_forms_for_dosing_review(drug_name, all_form_names_identified, GEMINI_API_KEY_GLOBAL)
        log_output(f"[INFO] /fetch_spl_suggestions: Gemini prioritized form names for {drug_name}: {prioritized_form_names}")
        
        # Ensure prioritized_form_names is always a list, even if select_forms_for_dosing_review returns None or error string
        if not isinstance(prioritized_form_names, list):
            log_output(f"[WARN] /fetch_spl_suggestions: select_forms_for_dosing_review did not return a list for {drug_name}. Result: {prioritized_form_names}. Defaulting to empty list for prioritization.")
            prioritized_form_names = []


        return jsonify(
            all_spls=all_forms_identified_with_spl_data,
            prioritized_form_names=prioritized_form_names,
            error=None
        )

    except Exception as e:
        log_output(f"[CRITICAL] Exception in /fetch_spl_suggestions for '{drug_name}': {str(e)}")
        import traceback
        log_output(traceback.format_exc())
        return jsonify(error=f"An unexpected error occurred while fetching SPL suggestions: {str(e)}"), 500

if __name__ == '__main__':
    print(f"Starting Flask server. Open http://127.0.0.1:5000 in your browser.")
    app.run(debug=True) # debug=True is helpful for development 