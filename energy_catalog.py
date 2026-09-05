"""Search and normalize energy drink data from Open Food Facts."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
SOURCE_URL = "https://world.openfoodfacts.org"
USER_AGENT = "EnergyCounterBot/1.0 (personal Telegram bot)"


class CatalogUnavailable(Exception):
    """Raised when the public product catalog cannot be reached."""


@dataclass(frozen=True)
class EnergyDrink:
    code: str
    name: str
    brand: str
    quantity: str
    caffeine_mg: float | None
    taurine_mg: float | None
    source_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "brand": self.brand,
            "quantity": self.quantity,
            "caffeine_mg": self.caffeine_mg,
            "taurine_mg": self.taurine_mg,
            "source_url": self.source_url,
        }


def search_energy_drink(query: str) -> EnergyDrink | None:
    params = {
        "search_terms": query,
        "search_simple": "1",
        "action": "process",
        "json": "1",
        "page_size": "10",
        "fields": (
            "code,product_name,product_name_en,brands,quantity,serving_size,"
            "nutriments,ingredients_text"
        ),
    }
    request_url = f"{SEARCH_URL}?{urlencode(params)}"
    data = _fetch_json(request_url)
    products = data.get("products", [])
    if not isinstance(products, list):
        return None

    best_product = _pick_best_product(query, products)
    if best_product is None:
        return None
    return _product_to_drink(best_product)


def _fetch_json(url: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            request = Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
            with urlopen(request, timeout=20) as response:
                result = json.load(response)
            if isinstance(result, dict):
                return result
            return {}
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))

    raise CatalogUnavailable from last_error


def _pick_best_product(query: str, products: list[Any]) -> dict[str, Any] | None:
    query_tokens = _tokens(query)
    candidates: list[tuple[int, dict[str, Any]]] = []

    for product in products:
        if not isinstance(product, dict):
            continue
        name = _first_text(
            product.get("product_name"),
            product.get("product_name_en"),
            product.get("generic_name"),
        )
        if not name:
            continue

        searchable_text = _normalize(
            " ".join(
                [
                    name,
                    _first_text(product.get("brands")),
                    _first_text(product.get("ingredients_text")),
                ]
            )
        )
        score = sum(3 if token in _normalize(name).split() else 1 for token in query_tokens)
        score += sum(1 for token in query_tokens if token in searchable_text)
        if query_tokens and score == 0:
            continue
        if "energy" in searchable_text or "энерг" in searchable_text:
            score += 2
        candidates.append((score, product))

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _product_to_drink(product: dict[str, Any]) -> EnergyDrink:
    code = _first_text(product.get("code"), product.get("_id")) or "unknown"
    name = _first_text(
        product.get("product_name"),
        product.get("product_name_en"),
        product.get("generic_name"),
    ) or "Без названия"
    brand = _first_text(product.get("brands")) or "Бренд не указан"
    quantity = _first_text(
        product.get("serving_size"),
        product.get("quantity"),
    ) or "Объём не указан"
    serving_ml = _extract_ml(
        _first_text(product.get("serving_size"), product.get("quantity"))
    )
    nutriments = product.get("nutriments")
    if not isinstance(nutriments, dict):
        nutriments = {}
    ingredients = _first_text(product.get("ingredients_text"))

    caffeine_mg = _find_nutrient(
        nutriments,
        "caffeine",
        serving_ml,
        ingredients,
    )
    taurine_mg = _find_nutrient(
        nutriments,
        "taurine",
        serving_ml,
        ingredients,
    )

    return EnergyDrink(
        code=code,
        name=name,
        brand=brand,
        quantity=quantity,
        caffeine_mg=caffeine_mg,
        taurine_mg=taurine_mg,
        source_url=f"{SOURCE_URL}/product/{code}",
    )


def _find_nutrient(
    nutriments: dict[str, Any],
    nutrient: str,
    serving_ml: float | None,
    ingredients: str,
) -> float | None:
    serving_value = _read_nutrient(nutriments, f"{nutrient}_serving", nutrient)
    if serving_value is not None:
        return serving_value

    per_100g_value = _read_nutrient(nutriments, f"{nutrient}_100g", nutrient)
    if per_100g_value is not None and serving_ml is not None:
        return per_100g_value * serving_ml / 100

    return _read_ingredient_nutrient(ingredients, nutrient)


def _read_nutrient(
    nutriments: dict[str, Any],
    key: str,
    nutrient: str,
) -> float | None:
    value = _as_float(nutriments.get(key))
    if value is None:
        return None
    unit = _first_text(
        nutriments.get(f"{key}_unit"),
        nutriments.get(f"{nutrient}_unit"),
    ) or "mg"
    return _to_mg(value, unit)


def _read_ingredient_nutrient(text: str, nutrient: str) -> float | None:
    if not text:
        return None
    aliases = {
        "caffeine": r"(?:caffeine|кофеин)",
        "taurine": r"(?:taurine|таурин)",
    }
    alias = aliases[nutrient]
    number_pattern = r"(\d+(?:[.,]\d+)?)\s*(mg|мг|g|г|µg|мкг|ug|mcg)?"
    patterns = (
        rf"{alias}.{{0,35}}?{number_pattern}",
        rf"{number_pattern}.{{0,35}}?{alias}",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = _as_float(match.group(1))
            if value is not None:
                return _to_mg(value, match.group(2) or "mg")
    return None


def _extract_ml(text: str) -> float | None:
    if not text:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(ml|мл|l|л)", text, re.IGNORECASE)
    if not match:
        return None
    value = _as_float(match.group(1))
    if value is None:
        return None
    return value * 1000 if match.group(2).lower() in {"l", "л"} else value


def _to_mg(value: float, unit: str) -> float:
    normalized_unit = unit.lower().replace("μ", "µ")
    if normalized_unit in {"g", "г"}:
        return value * 1000
    if normalized_unit in {"µg", "мкг", "ug", "mcg"}:
        return value / 1000
    return value


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _first_text(*values: Any) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _normalize(value: str) -> str:
    return re.sub(r"[^\w\s]+", " ", value.lower(), flags=re.UNICODE).strip()


def _tokens(value: str) -> list[str]:
    return [token for token in _normalize(value).split() if len(token) > 1]