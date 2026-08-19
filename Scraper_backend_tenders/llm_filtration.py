# llm_analytical_scorer.py
from dotenv import load_dotenv

load_dotenv()

import os
import json
import re
import time
import threading
import pandas as pd
from datetime import datetime
import requests
from openpyxl.styles import PatternFill
from google import genai
from google.genai import types
from google.genai.errors import ClientError

# =====================================================================
# PART 0: GEMINI API KEY CONFIG + ROTATION (same pattern as main.py)
# =====================================================================
# Set GEMINI_API_KEYS="key1,key2" in .env, or fall back to a single GEMINI_API_KEY.
_raw_keys = os.environ.get("GEMINI_API_KEYS", "") or os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]

if GEMINI_API_KEYS:
    masked = ", ".join(f"{k[:8]}..." for k in GEMINI_API_KEYS)
    print(f"[Config] Gemini keys: SET ({len(GEMINI_API_KEYS)} key(s): {masked})")
else:
    print("[Config] Gemini keys: MISSING")


class GeminiKeyRotator:
    """
    Thread-safe rotation across multiple Gemini API keys.
    A 429 is treated as a TEMPORARY, time-based cooldown (RPM limits reset on
    a rolling ~60s window) rather than a permanent block for the rest of the run.
    """

    COOLDOWN_SECONDS = 65  # slightly over the typical 60s RPM window

    def __init__(self, keys):
        self._keys = keys
        self._lock = threading.Lock()
        self._index = 0
        self._cooldown_until = {}  # index -> unix timestamp when it's usable again
        self._clients = {}  # index -> cached genai.Client for that key

    def _is_available(self, index):
        until = self._cooldown_until.get(index)
        return until is None or time.time() >= until

    def current(self):
        with self._lock:
            if not self._keys:
                return None, None, None
            if self._is_available(self._index):
                idx = self._index
            else:
                idx = None
                for i in range(len(self._keys)):
                    if self._is_available(i):
                        idx = i
                        self._index = i
                        break
                if idx is None:
                    idx = min(self._cooldown_until, key=self._cooldown_until.get)
            if idx not in self._clients:
                self._clients[idx] = genai.Client(api_key=self._keys[idx])
            return idx, self._keys[idx], self._clients[idx]

    def mark_exhausted(self, index):
        """Put a key on cooldown and advance to the next available key."""
        with self._lock:
            self._cooldown_until[index] = time.time() + self.COOLDOWN_SECONDS
            for i in range(len(self._keys)):
                if self._is_available(i):
                    self._index = i
                    return True
            return False  # every key is currently on cooldown

    def all_exhausted(self):
        with self._lock:
            return not any(self._is_available(i) for i in range(len(self._keys)))

    def seconds_until_next_available(self):
        with self._lock:
            if not self._cooldown_until:
                return 0
            return max(0, min(self._cooldown_until.values()) - time.time())

    def key_count(self):
        return len(self._keys)


gemini_rotator = GeminiKeyRotator(GEMINI_API_KEYS)


def _is_rate_limit_error(error) -> bool:
    if isinstance(error, ClientError) and getattr(error, "code", None) == 429:
        return True
    msg = str(error)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "rate limit" in msg.lower()


def generate_with_rotation(model, contents, config):
    """
    Calls client.models.generate_content, rotating across configured Gemini
    keys on a 429 instead of failing the tender outright. Mirrors the
    extract_fields_with_gemini retry behavior in main.py.
    """
    attempts = max(gemini_rotator.key_count(), 1) + 1  # +1 for the cooldown-wait retry

    for _ in range(attempts):
        key_index, api_key, client = gemini_rotator.current()
        if client is None:
            raise RuntimeError("No Gemini API keys configured (set GEMINI_API_KEYS in .env).")

        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except Exception as error:
            if _is_rate_limit_error(error):
                print(f"  [Gemini] Key #{key_index} ({api_key[:8]}...) rate-limited (429).")
                still_have_keys = gemini_rotator.mark_exhausted(key_index)
                if still_have_keys:
                    print("  [Gemini] Switching to next key and retrying same request...")
                    continue
                wait_s = min(gemini_rotator.seconds_until_next_available(), 65)
                if wait_s > 0:
                    print(f"  [Gemini] All keys on cooldown — waiting {wait_s:.0f}s for RPM window to reset...")
                    time.sleep(wait_s)
                    continue
                raise
            raise  # not a rate-limit issue — let the caller's except block handle it

    raise RuntimeError("Gemini scoring failed after exhausting all keys and retries.")


# =====================================================================
# PART 1: LLM ANALYTICAL SCORER ENGINE (UNTOUCHED RUBRIC)
# =====================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
json_file = os.path.join(BASE_DIR, "all_tenders.json")

def evaluate_and_score_tenders(input_json_path=os.path.join(BASE_DIR, "all_tenders.json"), output_json_path=os.path.join(BASE_DIR, "all_tenders.json")):

    """
    Reads semantically filtered tenders matching the scraper layout, runs them
    through the 2-Dimension Technical Fit Rubric via Gemini 2.5, appends
    the LLM_RelevancyScore, and outputs the exact schema structure to disk.
    """
    if not os.path.exists(input_json_path):
        print(f"Execution Halted: Input file '{input_json_path}' cannot be located.")
        return False

    with open(input_json_path, "r") as f:
        tenders = json.load(f)

    if not tenders:
        print(f"⚠️ Operations halted: Target input JSON file '{input_json_path}' is empty.")
        return False

    print(f" Successfully loaded {len(tenders)} tenders from semantic cache.")
    print(" Commencing Multi-Criteria Architectural Analysis using Gemini 2.5 Engine...\n")

    system_instruction = """
You are a strict, precise technical auditing system. Your sole task is to evaluate an incoming tender JSON object against an uncompromising 10.0-Point scoring rubric, calculate an exact composite float score out of 10.0, and append that calculated score to the JSON object under the key "LLM_RelevancyScore".

Do NOT rewrite the tender.

Evaluate it using the scoring rubric.

Return ONLY

{
  "LLM_RelevancyScore": <float>
}
CRITICAL CORE DIRECTION:
- Evaluate the tender EXCLUSIVELY based on the structural criteria provided below.
- You are completely open to custom product development, new product lines, and alternative form factors. Do not penalize a tender for proposing a brand-new type of device or hardware as long as it tracks sensor metrics.
- Completely ignore the volume/scale of deployment, the geographical location, or who the buying entity is.
- You must be uncompromisingly strict: do not assume or infer capabilities. Score only what is explicitly stated in the text.

=====================================================================
SCORING MATRIX (MAXIMUM 10.0 POINTS)
=====================================================================

--- DIMENSION 1: CORE TRACKING & SENSOR REQUIREMENT (0.0 to 5.0 Points) ---
This dimension evaluates if the project fundamentally revolves around collecting data via sensor hardware.

* 5.0 Points: Direct Sensor/Tracking Match
  The tender explicitly mandates tracking, measuring, or collecting data using physical hardware, sensors, biometric components, wearables, portable trackers, or data-collection points. This applies across any sector (Health, Defence, Corporate, Pet tracking, or an entirely new tracking domain).

* 0.0 Points: Unrelated Categories / No Sensors
  The project is for a completely unrelated infrastructure, hardware, or services with zero tracking sensor context. Examples include fixed CCTV optical security camera networks, physical perimeter fencing, construction/civil engineering, brick-and-mortar office furniture supply, or general administrative staffing.

--- DIMENSION 2: SENSIO AI LINGUISTIC & TARGET TRACK MATCH (0.0 to 2.5 Points) ---
This dimension utilizes your semantic understanding to evaluate if the descriptive vocabulary matches the core target tracking ecosystems of Sensio AI.

* 2.5 Points: High-Value Target Domain Context
  The text utilizes vocabulary, terminology, or nomenclature that aligns with any of the following four primary semantic tracking tracks. It does not require exact word matching; evaluate based on context, synonyms, and operational intent:

  1. The Physiological/Clinical Track: Language referencing human health status, patient diagnostics, clinical monitoring, vital sign acquisition, medical trials, or biometric data logging.
  2. The Tactical/Operational Track: Language referencing personnel field tracking, soldier readiness, infantry deployment, emergency first responder status, or physical strain monitoring under stressful operations.
  3. The Workforce/Occupational Track: Language referencing everyday corporate employee metrics, corporate wellness programs, desk-bound staff activity tracking, office lifestyle optimization, or personnel movement analytics within a business infrastructure.
  4. The Animal/Veterinary Track: Language referencing canine, feline, livestock, or service animal tracking, biological vitals for animals, or specialized mobile tracking frameworks like smart collars or animal harnesses.

* 0.0 Points: Industrial / Static Asset Context
  The text completely lacks any human, biological, or personal workforce tracking context. It describes tracking inanimate, industrial, or mass logistics objects (e.g., shipping crates, fleet vehicles, factory raw materials) using purely industrial asset management phrasing, without any crossover into Sensio AI's human, employee, or animal tracking tracks.

--- DIMENSION 3: CONNECTIVITY & DATA MOBILITY (0.0 to 2.5 Points) ---
This dimension evaluates the operational freedom of data collection and transmission.

* 2.5 Points: Mobile / Wireless Transmission
  The tender requires data to move wirelessly via any modern broadcasting protocols (e.g., Bluetooth/BLE, Wi-Fi, Cellular LTE/5G, Mesh topologies, or Satellite communication) back to a smartphone application, desktop dashboard, cloud ecosystem, or external API endpoints.

* 0.0 Points: Strictly Hard-Wired / Static Tethering
  The tracking hardware is explicitly restricted to fixed, hard-wired connections (e.g., a desktop device that must remain continuously plugged into a physical PC via a USB cable to operate) with zero mobile capability or wireless data broadcasting networks.

=====================================================================
CRITICAL OUTPUT PROTOCOL (UNCOMPROMISING CONSTRAINT)
=====================================================================
- You must output VALID JSON ONLY.
- Do NOT include markdown blocks like ```json or ``` outside of the object.
- Do NOT provide conversational greetings, text introductions, notes, or concluding remarks.
- Do NOT provide reasoning, explanations, justifications, or a breakdown of how the score was calculated.
- Do NOT modify, alter, delete, or reorder any existing key-value pairs inside the incoming JSON object.
- Your output must consist EXCLUSIVELY of the original input fields, with the single addition of the "LLM_RelevancyScore" key populated with your final float value (e.g., 7.5 or 10.0). Any deviation from this rule will break the automated pipeline parser.
"""

    # response_schema = types.Schema(
    #     type=types.Type.OBJECT,
    #     properties={
    #         "Primary Key": types.Schema(type=types.Type.STRING),
    #         "Adapter": types.Schema(type=types.Type.STRING),
    #         "Country": types.Schema(type=types.Type.STRING),
    #         "Sector": types.Schema(type=types.Type.STRING),
    #         "Health or Defence Category": types.Schema(type=types.Type.STRING),
    #         "Budget Currency": types.Schema(type=types.Type.STRING),
    #         "Budget in Local Currency Minimum": types.Schema(type=types.Type.STRING),
    #         "Budget in Local Currency Maximum": types.Schema(type=types.Type.STRING),
    #         "Budget in INR Minimum": types.Schema(type=types.Type.STRING),
    #         "Budget in INR Maximum": types.Schema(type=types.Type.STRING),
    #         "Order Quantity": types.Schema(type=types.Type.STRING),
    #         "Expiry Date": types.Schema(type=types.Type.STRING),
    #         "Opening Date": types.Schema(type=types.Type.STRING),
    #         "Organisation Name": types.Schema(type=types.Type.STRING),
    #         "Link to the Tender": types.Schema(type=types.Type.STRING),
    #         "Tender Title": types.Schema(type=types.Type.STRING),
    #         "Tender Description": types.Schema(type=types.Type.STRING),
    #         "Special Observation": types.Schema(type=types.Type.STRING),
    #         "Award Date": types.Schema(type=types.Type.STRING),
    #         "Timeline": types.Schema(type=types.Type.STRING),
    #         "Eligibility": types.Schema(type=types.Type.STRING),
    #         "Keyword included": types.Schema(type=types.Type.STRING),
    #         "Application Status": types.Schema(type=types.Type.STRING),
    #         "Current Applicants": types.Schema(type=types.Type.STRING),
    #         "GeneratedAt": types.Schema(type=types.Type.STRING),
    #         "LLM_RelevancyScore": types.Schema(
    #             type=types.Type.NUMBER,
    #             description="The calculated objective relevancy score as a float locked strictly between 0.0 and 10.0"
    #         )
    #     }
    # )

    response_schema = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "LLM_RelevancyScore": types.Schema(
            type=types.Type.NUMBER
        )
    }
)

    enriched_dataset = []

    # for index, tender in enumerate(tenders, start=1):
    #     # tender_title = tender.get("Tender Title", "Unknown Identifier")
    #     tender_title = str(tender.get("Tender Title") or "Unknown Identifier")
    #     print(f" Processing Record {index}/{len(tenders)}: '{tender_title[:45]}...'")

    #     for key, value in tender.items():
    #         if value is None:
    #             tender[key] = "None"

    for index, tender in enumerate(tenders, start=1):

        # Replace None values with empty strings
        for key, value in tender.items():
            if value is None:
                tender[key] = ""

        # Make sure important text fields are always strings
        tender["Tender Title"] = str(tender.get("Tender Title") or "")
        tender["Tender Description"] = str(tender.get("Tender Description") or "")
        tender["Organisation Name"] = str(tender.get("Organisation Name") or "")

        tender_title = tender["Tender Title"] or "Unknown Identifier"

        print(f" Processing Record {index}/{len(tenders)}: '{tender_title[:45]}...'")

        if not tender["Tender Title"]:
            print(f"\n⚠️ Tender #{index} has no title:")
            print(json.dumps(tender, indent=2))

        structured_prompt = (
            f"Please run our 4-pillar architectural assessment matrix against this incoming tender data record. "
            f"Analyze the text and populate the 'LLM_RelevancyScore' float fields:\n\n"
            f"{json.dumps(tender, indent=2)}"
        )

        try:
            response = generate_with_rotation(
                model='gemini-3.6-flash',
                contents=structured_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=response_schema,
                    temperature=0.0,
                ),
            )

            # evaluated_item = json.loads(response.text)
            # enriched_dataset.append(evaluated_item)
            score = json.loads(response.text)

            # Only copy the score into the original tender
            tender["LLM_RelevancyScore"] = score.get(
                "LLM_RelevancyScore",
                0.0
            )

            enriched_dataset.append(tender)

        except Exception as error:
            print(f"  ⚠️ Error encountered while scoring item {index}. Injecting fallback baseline score. Detail: {error}")
            tender["LLM_RelevancyScore"] = 0.0
            enriched_dataset.append(tender)

    with open(output_json_path, "w") as f:
        json.dump(enriched_dataset, f, indent=2)

    print("\n" + "="*60)
    print(f"  ANALYSIS COMPLETION SUCCESS!")
    print(f" Saved precisely scored dataset out to: '{output_json_path}'")
    print("="*60 + "\n")
    return True

if __name__ == "__main__":
    evaluate_and_score_tenders(
        input_json_path=json_file,
        output_json_path=json_file
    )

# # llm_analytical_scorer.py
# # code wont work as you must insert an API key
# import os
# import json
# import re
# import pandas as pd
# from datetime import datetime
# import requests
# from openpyxl.styles import PatternFill
# from google import genai
# from google.genai import types

# # =====================================================================
# # PART 1: LLM ANALYTICAL SCORER ENGINE (UNTOUCHED RUBRIC)
# # =====================================================================
# def evaluate_and_score_tenders(input_json_path="all_tenders.json", output_json_path="all_tenders.json"):
#     """
#     Reads semantically filtered tenders matching the scraper layout, runs them
#     through the 2-Dimension Technical Fit Rubric via Gemini 2.5, appends
#     the LLM_RelevancyScore, and outputs the exact schema structure to disk.
#     """
#     client = genai.Client(api_key="") # IMPORTANT add your api key here from google ai studio

#     if not os.path.exists(input_json_path):
#         print(f"Execution Halted: Input file '{input_json_path}' cannot be located.")
#         return False

#     with open(input_json_path, "r") as f:
#         tenders = json.load(f)

#     if not tenders:
#         print(f"⚠️ Operations halted: Target input JSON file '{input_json_path}' is empty.")
#         return False

#     print(f" Successfully loaded {len(tenders)} tenders from semantic cache.")
#     print(" Commencing Multi-Criteria Architectural Analysis using Gemini 2.5 Engine...\n")

#     system_instruction = """
# You are a strict, precise technical auditing system. Your sole task is to evaluate an incoming tender JSON object against an uncompromising 10.0-Point scoring rubric, calculate an exact composite float score out of 10.0, and append that calculated score to the JSON object under the key "LLM_RelevancyScore".

# CRITICAL CORE DIRECTION:
# - Evaluate the tender EXCLUSIVELY based on the structural criteria provided below.
# - You are completely open to custom product development, new product lines, and alternative form factors. Do not penalize a tender for proposing a brand-new type of device or hardware as long as it tracks sensor metrics.
# - Completely ignore the volume/scale of deployment, the geographical location, or who the buying entity is.
# - You must be uncompromisingly strict: do not assume or infer capabilities. Score only what is explicitly stated in the text.

# =====================================================================
# SCORING MATRIX (MAXIMUM 10.0 POINTS)
# =====================================================================

# --- DIMENSION 1: CORE TRACKING & SENSOR REQUIREMENT (0.0 to 5.0 Points) ---
# This dimension evaluates if the project fundamentally revolves around collecting data via sensor hardware.

# * 5.0 Points: Direct Sensor/Tracking Match
#   The tender explicitly mandates tracking, measuring, or collecting data using physical hardware, sensors, biometric components, wearables, portable trackers, or data-collection points. This applies across any sector (Health, Defence, Corporate, Pet tracking, or an entirely new tracking domain).

# * 0.0 Points: Unrelated Categories / No Sensors
#   The project is for a completely unrelated infrastructure, hardware, or services with zero tracking sensor context. Examples include fixed CCTV optical security camera networks, physical perimeter fencing, construction/civil engineering, brick-and-mortar office furniture supply, or general administrative staffing.

# --- DIMENSION 2: SENSIO AI LINGUISTIC & TARGET TRACK MATCH (0.0 to 2.5 Points) ---
# This dimension utilizes your semantic understanding to evaluate if the descriptive vocabulary matches the core target tracking ecosystems of Sensio AI.

# * 2.5 Points: High-Value Target Domain Context
#   The text utilizes vocabulary, terminology, or nomenclature that aligns with any of the following four primary semantic tracking tracks. It does not require exact word matching; evaluate based on context, synonyms, and operational intent:

#   1. The Physiological/Clinical Track: Language referencing human health status, patient diagnostics, clinical monitoring, vital sign acquisition, medical trials, or biometric data logging.
#   2. The Tactical/Operational Track: Language referencing personnel field tracking, soldier readiness, infantry deployment, emergency first responder status, or physical strain monitoring under stressful operations.
#   3. The Workforce/Occupational Track: Language referencing everyday corporate employee metrics, corporate wellness programs, desk-bound staff activity tracking, office lifestyle optimization, or personnel movement analytics within a business infrastructure.
#   4. The Animal/Veterinary Track: Language referencing canine, feline, livestock, or service animal tracking, biological vitals for animals, or specialized mobile tracking frameworks like smart collars or animal harnesses.

# * 0.0 Points: Industrial / Static Asset Context
#   The text completely lacks any human, biological, or personal workforce tracking context. It describes tracking inanimate, industrial, or mass logistics objects (e.g., shipping crates, fleet vehicles, factory raw materials) using purely industrial asset management phrasing, without any crossover into Sensio AI's human, employee, or animal tracking tracks.

# --- DIMENSION 3: CONNECTIVITY & DATA MOBILITY (0.0 to 2.5 Points) ---
# This dimension evaluates the operational freedom of data collection and transmission.

# * 2.5 Points: Mobile / Wireless Transmission
#   The tender requires data to move wirelessly via any modern broadcasting protocols (e.g., Bluetooth/BLE, Wi-Fi, Cellular LTE/5G, Mesh topologies, or Satellite communication) back to a smartphone application, desktop dashboard, cloud ecosystem, or external API endpoints.

# * 0.0 Points: Strictly Hard-Wired / Static Tethering
#   The tracking hardware is explicitly restricted to fixed, hard-wired connections (e.g., a desktop device that must remain continuously plugged into a physical PC via a USB cable to operate) with zero mobile capability or wireless data broadcasting networks.

# =====================================================================
# CRITICAL OUTPUT PROTOCOL (UNCOMPROMISING CONSTRAINT)
# =====================================================================
# - You must output VALID JSON ONLY.
# - Do NOT include markdown blocks like ```json or ``` outside of the object.
# - Do NOT provide conversational greetings, text introductions, notes, or concluding remarks.
# - Do NOT provide reasoning, explanations, justifications, or a breakdown of how the score was calculated.
# - Do NOT modify, alter, delete, or reorder any existing key-value pairs inside the incoming JSON object.
# - Your output must consist EXCLUSIVELY of the original input fields, with the single addition of the "LLM_RelevancyScore" key populated with your final float value (e.g., 7.5 or 10.0). Any deviation from this rule will break the automated pipeline parser.
# """

#     response_schema = types.Schema(
#         type=types.Type.OBJECT,
#         properties={
#             "Primary Key": types.Schema(type=types.Type.STRING),
#             "Adapter": types.Schema(type=types.Type.STRING),
#             "Country": types.Schema(type=types.Type.STRING),
#             "Sector": types.Schema(type=types.Type.STRING),
#             "Health or Defence Category": types.Schema(type=types.Type.STRING),
#             "Budget Currency": types.Schema(type=types.Type.STRING),
#             "Budget in Local Currency Minimum": types.Schema(type=types.Type.STRING),
#             "Budget in Local Currency Maximum": types.Schema(type=types.Type.STRING),
#             "Budget in INR Minimum": types.Schema(type=types.Type.STRING),
#             "Budget in INR Maximum": types.Schema(type=types.Type.STRING),
#             "Order Quantity": types.Schema(type=types.Type.STRING),
#             "Expiry Date": types.Schema(type=types.Type.STRING),
#             "Opening Date": types.Schema(type=types.Type.STRING),
#             "Organisation Name": types.Schema(type=types.Type.STRING),
#             "Link to the Tender": types.Schema(type=types.Type.STRING),
#             "Tender Title": types.Schema(type=types.Type.STRING),
#             "Tender Description": types.Schema(type=types.Type.STRING),
#             "Special Observation": types.Schema(type=types.Type.STRING),
#             "Award Date": types.Schema(type=types.Type.STRING),
#             "Timeline": types.Schema(type=types.Type.STRING),
#             "Eligibility": types.Schema(type=types.Type.STRING),
#             "Keyword included": types.Schema(type=types.Type.STRING),
#             "Application Status": types.Schema(type=types.Type.STRING),
#             "Current Applicants": types.Schema(type=types.Type.STRING),
#             "LLM_RelevancyScore": types.Schema(
#                 type=types.Type.NUMBER,
#                 description="The calculated objective relevancy score as a float locked strictly between 0.0 and 10.0"
#             )
#         }
#     )

#     enriched_dataset = []

#     for index, tender in enumerate(tenders, start=1):
#         tender_title = tender.get("Tender Title", "Unknown Identifier")
#         print(f" Processing Record {index}/{len(tenders)}: '{tender_title[:45]}...'")

#         for key, value in tender.items():
#             if value is None:
#                 tender[key] = "None"

#         structured_prompt = (
#             f"Please run our 4-pillar architectural assessment matrix against this incoming tender data record. "
#             f"Analyze the text and populate the 'LLM_RelevancyScore' float fields:\n\n"
#             f"{json.dumps(tender, indent=2)}"
#         )

#         try:
#             response = client.models.generate_content(
#                 model='gemini-2.5-flash',
#                 contents=structured_prompt,
#                 config=types.GenerateContentConfig(
#                     system_instruction=system_instruction,
#                     response_mime_type="application/json",
#                     response_schema=response_schema,
#                     temperature=0.0,
#                 ),
#             )

#             evaluated_item = json.loads(response.text)
#             enriched_dataset.append(evaluated_item)

#         except Exception as error:
#             print(f"  ⚠️ Error encountered while scoring item {index}. Injecting fallback baseline score. Detail: {error}")
#             tender["LLM_RelevancyScore"] = 0.0
#             enriched_dataset.append(tender)

#     with open(output_json_path, "w") as f:
#         json.dump(enriched_dataset, f, indent=2)

#     print("\n" + "="*60)
#     print(f"  ANALYSIS COMPLETION SUCCESS!")
#     print(f" Saved precisely scored dataset out to: '{output_json_path}'")
#     print("="*60 + "\n")
#     return True

# if __name__ == "__main__":
#     evaluate_and_score_tenders(
#         input_json_path="all_tenders.json",
#         output_json_path="all_tenders.json"
#     )
