# Eco-Travel Advisor (Rasa 3.6)

A Rasa Open Source assistant that plans lower-carbon trips: adaptive trip intake, journey emissions (local factor table, optional Climatiq), OpenStreetMap hotels/transport/attractions, Wikipedia, Open-Meteo weather, Frankfurter currency, weighted scoring, colour-coded results, GDPR-style consented human handover and a two-stage fallback.

## Layout
| Path | Purpose |
|---|---|
| `config.yml` | NLU pipeline (DIET + FallbackClassifier) and policies (Rules, TED, UnexpecTED) |
| `domain.yml`, `data/*.yml` | Intents, entities, slots, `trip_form`, responses, NLU data, rules, stories |
| `actions/actions.py` | Form validation, `action_search_options`, `action_resolve_location`, `action_handover`, `action_default_fallback` |
| `frontend/index.html` | Accessible chat UI over the REST channel (buttons, result cards, alerts, handover banner, geolocation) |
| `tests/test_stories.yml` | Dialogue test stories |
| `Dockerfile`, `start.sh`, `nginx.conf` | Single-container deployment (Spaces-compatible, port 7860) |

## Run locally (Python 3.10)
```bash
python3.10 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set CONTACT_EMAIL; CLIMATIQ_API_KEY is optional
set -a && source .env && set +a
rasa train
rasa run actions --port 5055 &
rasa run --enable-api --cors "*" --port 5005 &
# open frontend/index.html after adding in the page:  <script>window.RASA_URL="http://localhost:5005/webhooks/rest/webhook"</script>
rasa shell                      # or chat in the terminal instead
```

## Testing
```bash
rasa data validate
rasa test nlu --nlu data/nlu.yml --cross-validation --folds 5   # intent/entity metrics + confusion matrix in results/
rasa test core --stories tests/test_stories.yml                 # dialogue + fallback tests
```
Add your own varied stories to `tests/` for the extended-testing requirement, and run the usability survey separately.

## Docker / Hugging Face Spaces
```bash
docker compose up --build       # http://localhost:7860
```
For Spaces: create a Docker Space, push this repo, add the keys from `.env.example` under Settings > Secrets. nginx exposes only `/webhooks/` (rate-limited); Rasa's admin API is not enabled in the container. For dev tunnelling, `pyngrok` can expose port 5005.

## Data sources (all key-free except optional Climatiq)
| Need | Source | Module function |
|---|---|---|
| Place to coordinates | Nominatim (1 req/s, cached) | `geocode` |
| Hotels, rail/metro/tram, museums and sights | Overpass / OpenStreetMap | `find_hotels`, `find_transport`, `find_attractions` |
| Place descriptions | Wikipedia REST (CC BY-SA, attributed) | `describe` |
| Weather | Open-Meteo | `get_weather` |
| Currency | Frankfurter (ECB daily reference rates) | `get_rate` |
| Carbon | Local factor table; Climatiq if a key is set | `carbon_kg` |

Amadeus is **not used**: your course notes say it was decommissioned on 17 July 2026.

## Design notes and known limits
- **Scoring** (`WEIGHTS` in `actions.py`): carbon / price / preference-fit weights depend on the sustainability level. Journey carbon is from the factor table; internal logic works in EUR and converts only for display.
- **Hotels**: OSM has no prices and almost no eco tags. Only hotels with a `green_key`/`ecolabel` tag are called certified. Others are ranked by proxies (distance to a station/stop, star tier as a price-tier stand-in) and labelled "Not certified". Grey card = no evidence either way.
- **False precision**: kg figures are rounded and described as "about"; car assumes one traveller (the bot does not yet ask party size).
- **Emission factors** are the rounded teaching values from your file 08. Replace them with cited DESNZ/DEFRA or EEA numbers before submitting.
- **Latency**: Nominatim's 1 req/s rule, shared Overpass servers and several lookups mean the 3-second target may be missed on a cold cache. Lookups run in parallel and are cached; measure it and report it honestly.
- **Ambiguous places**: the top Nominatim match is used and echoed back ("Understood as ..."). Disambiguation buttons are a worthwhile next step.
- **Not implemented**: voice input, multi-language, DistilBERT featurizer (commented in `config.yml`), party size.
