import os
import json
import re
import requests

from energy_catalog import EnergyDrink

SEARX = "https://searx.be/search"
OR = "https://openrouter.ai/api/v1/chat/completions"


def ai_search_energy(q: str):
    try:
        print("AI SEARCH:", q)

        key = os.getenv("OPENROUTER_API_KEY")

        if not key:
            print("ERROR: OPENROUTER_API_KEY not found")
            return None

        print("Searching SearX...")

        response = requests.get(
            SEARX,
            params={
                "q": q + " energy drink caffeine taurine volume",
                "format": "json"
            },
            timeout=12
        )

        print("SearX status:", response.status_code)

        data = response.json()

        results = data.get("results", [])

        if not results:
            print("No search results")
            return None

        text = "\n".join(
            [
                f"{x.get('title','')}\n{x.get('content','')}"
                for x in results[:5]
            ]
        )

        prompt = f"""
Extract JSON from this information.

Return ONLY:
{{
"name":"",
"brand":"",
"quantity":"",
"caffeine_mg":0,
"taurine_mg":0
}}

Information:
{text}
"""

        print("Sending request to OpenRouter...")

        ai = requests.post(
            OR,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json"
            },
            json={
                "model": "google/gemma-3-27b-it:free",
                "messages": [
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            },
            timeout=30
        )

        print("OpenRouter status:", ai.status_code)

        result = ai.json()

        content = result["choices"][0]["message"]["content"]

        print("AI answer:", content)

        match = re.search(r"\{.*\}", content, re.S)

        if not match:
            print("JSON not found")
            return None

        item = json.loads(match.group())

        return EnergyDrink(
            code="ai-" + q,
            name=item.get("name", q),
            brand=item.get("brand", "?"),
            quantity=item.get("quantity", "?"),
            caffeine_mg=float(item.get("caffeine_mg", 0)),
            taurine_mg=float(item.get("taurine_mg", 0)),
            source_url=SEARX
        )

    except Exception as e:
        print("AI ERROR:", repr(e))
        return None
