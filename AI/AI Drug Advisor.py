"""
AI-анализ взаимодействий лекарств
"""
import json
from typing import List, Dict
from .openrouter import openrouter_client

async def check_drug_interactions(
    new_drug: str, 
    existing_drugs: List[str], 
    user_age: int
) -> Dict:
    """Проверка взаимодействий с AI"""
    
    prompt = f"""Ты фармацевт-консультант. Проанализируй добавление препарата.

НОВЫЙ ПРЕПАРАТ: {new_drug}
ТЕКУЩИЕ ПРЕПАРАТЫ: {', '.join(existing_drugs) if existing_drugs else 'нет'}
ВОЗРАСТ ПАЦИЕНТА: {user_age} лет

Проверь:
1. Совместимость с текущими препаратами (опасные взаимодействия?)
2. Оптимальное время приёма (утро/день/вечер)
3. Приём относительно еды (до/после/во время)
4. Основные побочные эффекты (2-3 пункта)

Формат ответа (только JSON, без дополнительного текста):
{{
  "compatibility": "safe|warning|danger",
  "interactions": "описание или 'нет опасных взаимодействий'",
  "best_time": "утро|день|вечер|любое время",
  "food_timing": "до еды|после еды|во время еды|не важно",
  "food_explanation": "краткое объяснение",
  "side_effects": ["побочка 1", "побочка 2", "побочка 3"],
  "recommendation": "итоговая рекомендация в 1-2 предложениях"
}}"""
    
    response = await openrouter_client.complete(
        prompt=prompt,
        response_format={"type": "json_object"}
    )
    
    return json.loads(response)
