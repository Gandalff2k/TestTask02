# Support drafts

Бекенд підтримки: приймає звернення з хелпдеску, генерує чернетку відповіді через LLM і базу знань, оператор її модерує, підтверджена відповідь іде клієнту.

Проєкт у розробці, повна інструкція з'явиться на етапі 7 ([план](docs/PLAN.md)).

## Запуск

```bash
docker compose up --build
```

Файл `.env` не обов'язковий. Без API-ключа сервіс працює в режимі статичної відповіді.

- API: http://localhost:8000/health
- Мок хелпдеску: http://localhost:8001/health

## Вибір LLM-провайдера

Скопіюй `.env.example` у `.env` і задай `LLM_PROVIDER` та `LLM_API_KEY`. Значення `LLM_PROVIDER`: `groq`, `gemini`, `openrouter`, `openai`, `custom`.
