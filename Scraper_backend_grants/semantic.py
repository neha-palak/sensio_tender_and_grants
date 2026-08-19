from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
import json, pandas as pd

from Scraper_backend_grants.target_profiles import HealthTargetProfiles, DefenceTargetProfiles, CorporateTargetProfiles, PetsTargetProfiles

import os

# Data files live in the Scraper_backend/ root, one level up from this scripts/ dir.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

model = SentenceTransformer('all-MiniLM-L6-v2')
print("✓ Model loaded")
print("Embedding dim:", model.get_sentence_embedding_dimension())

health_emb  = model.encode(HealthTargetProfiles,  normalize_embeddings=True)
defence_emb = model.encode(DefenceTargetProfiles, normalize_embeddings=True)
corporate_emb = model.encode(CorporateTargetProfiles, normalize_embeddings=True)
pets_emb = model.encode(PetsTargetProfiles, normalize_embeddings=True)

health_vec  = health_emb.mean(axis=0, keepdims=True)
defence_vec = defence_emb.mean(axis=0, keepdims=True)
corporate_vec = corporate_emb.mean(axis=0, keepdims=True)
pets_vec = pets_emb.mean(axis=0, keepdims=True)


def score_grant(grant: dict, threshold=0.35) -> dict:
    # text = f"{grant.get('GrantTitle', '')} {grant.get('WorkDescription', '')}"
    text = (
        f"{grant.get('Grant Title', '')} "
        f"{grant.get('Grant Description', '')}"
    )
    vec  = model.encode([text], normalize_embeddings=True)

    h_score = float(cosine_similarity(vec, health_vec)[0][0])
    d_score = float(cosine_similarity(vec, defence_vec)[0][0])
    c_score = float(cosine_similarity(vec, corporate_vec)[0][0])
    p_score = float(cosine_similarity(vec, pets_vec)[0][0])

    scores = {
        "Health"    : h_score,
        "Defence"   : d_score,
        "Corporate" : c_score,
        "Pets"      : p_score,
    }

    passing = {sector: score for sector, score in scores.items() if score >= threshold}
    top_sector = max(passing, key=passing.get) if passing else "None"

    return {
        "health_score"    : round(h_score, 4),
        "defence_score"   : round(d_score, 4),
        "corporate_score" : round(c_score, 4),
        "pet_score"       : round(p_score, 4),
        "health_pass"     : h_score >= threshold,
        "defence_pass"    : d_score >= threshold,
        "corporate_pass"  : c_score >= threshold,
        "pets_pass"       : p_score >= threshold,
        "sector"          : top_sector,
    }

print("✓ score_grant() ready")

# def semantic_filter(grants, threshold=0.35):
#     results = [score_grant(t, threshold) for t in grants]
#     df = pd.DataFrame(results)
#     print(df[[
#         'health_score', 'defence_score', 'corporate_score', 'pet_score',
#         'health_pass',  'defence_pass',  'corporate_pass',  'pets_pass',
#         'sector'
#     ]])

#     # filtered = [
#     #     {**grant, **result}
#     #     for grant, result in zip(grants, results)
#     #     if any([
#     #         result["health_pass"],
#     #         result["defence_pass"],
#     #         result["corporate_pass"],
#     #         result["pets_pass"],
#     #     ])
#     # ]
#     filtered = [
#         {**grant, **result}
#         for grant, result in zip(grants, results)
#     ]
#     print(f"Input grants: {len(grants)}")
#     print(f"Filtered grants: {len(filtered)}")
#     # with open("uk_file.json", "w") as f:
#     #     json.dump(filtered, f, indent=2)
#     import os

#     print("Writing JSON to:", os.path.abspath(output_file))

#     with open(output_file, "w") as f:
#         json.dump(filtered, f, indent=4)

#     print("Exists:", os.path.exists(output_file))
#     print("Size:", os.path.getsize(output_file), "bytes")
#     print("Modified:", datetime.fromtimestamp(os.path.getmtime(output_file)))

#     print(f"✓ Exported {len(filtered)} / {len(grants)} grants → uk_file.json")

from datetime import datetime

def semantic_filter(grants, threshold=0.35, output_file=None):
    
    # Default output location
    if output_file is None:
        output_file = os.path.join(BASE_DIR, "all_grants.json")

    results = [score_grant(t, threshold) for t in grants]
    run_timestamp = datetime.now().isoformat()

    df = pd.DataFrame(results)
    print(df[[
        'health_score', 'defence_score', 'corporate_score', 'pet_score',
        'health_pass', 'defence_pass', 'corporate_pass', 'pets_pass',
        'sector'
    ]])

    # filtered = [
    #     {**grant, **result}
    #     for grant, result in zip(grants, results)
    # ]
    filtered = [
        {
            **grant,
            **result,
            "GeneratedAt": run_timestamp
        }
        for grant, result in zip(grants, results)
    ]

    print(f"Input grants: {len(grants)}")
    print(f"Filtered grants: {len(filtered)}")

    print(f"Writing JSON to: {output_file}")

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(filtered, f, indent=4, ensure_ascii=False)

    print("Exists:", os.path.exists(output_file))
    print("Size:", os.path.getsize(output_file), "bytes")
    print("Modified:", datetime.fromtimestamp(os.path.getmtime(output_file)))

    print(f"✓ Exported {len(filtered)} / {len(grants)} grants → {output_file}")