# Musashi API README

Musashi exposes a public REST API for prediction-market intelligence on top of Polymarket and Kalshi.

This document covers:
- every currently implemented public API endpoint
- how to call each endpoint
- supported parameters
- response and error formats
- copy-paste examples in `curl`, JavaScript, and Python

## Base URL

Production:

```text
https://musashi-api.vercel.app
```

Local development:

```text
http://localhost:3000
```

If you run the Vercel dev server on a different port, replace the host accordingly.

## Authentication

No API key is required for the public REST endpoints documented here.

Musashi client libraries may send an `Authorization: Bearer ...` header, but the current public handlers do not require it.

## Transport Notes

- All endpoints return JSON.
- All endpoints support CORS.
- All endpoints respond to `OPTIONS` for browser preflight requests.
- Timestamps are ISO 8601 strings unless otherwise noted.
- Prices and confidence scores are decimals between `0` and `1`.

## Quick Start

### Analyze a piece of text

```bash
curl -X POST https://musashi-api.vercel.app/api/analyze-text \
  -H "Content-Type: application/json" \
  -d '{
    "text": "The Fed just hinted at a rate cut this summer.",
    "minConfidence": 0.3,
    "maxResults": 5
  }'
```

### Fetch arbitrage opportunities

```bash
curl "https://musashi-api.vercel.app/api/markets/arbitrage?minSpread=0.03&minConfidence=0.5&limit=10"
```

### Fetch the analyzed tweet feed

```bash
curl "https://musashi-api.vercel.app/api/feed?limit=20&category=crypto&minUrgency=high"
```

## Endpoints

### 1. `POST /api/analyze-text`

Analyzes a piece of text and returns matched markets plus a trading-oriented signal.

Typical inputs:
- tweets
- headlines
- breaking news alerts
- research notes
- trader commentary

#### Request Body

```json
{
  "text": "Bitcoin just broke above $100k.",
  "minConfidence": 0.3,
  "maxResults": 5
}
```

#### Parameters

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `text` | `string` | Yes | - | The text to analyze. Max length: `10000` characters. |
| `minConfidence` | `number` | No | `0.3` | Minimum market-match confidence. Must be between `0` and `1`. |
| `maxResults` | `number` | No | `5` | Maximum number of matched markets to return. Must be between `1` and `100`. |

#### Success Response

```json
{
  "event_id": "evt_k2s8k1",
  "signal_type": "news_event",
  "urgency": "high",
  "success": true,
  "data": {
    "markets": [
      {
        "market": {
          "id": "0x123",
          "platform": "polymarket",
          "title": "Will Bitcoin reach $100k by June 2026?",
          "description": "Resolves YES if Bitcoin touches $100k by the end date.",
          "keywords": ["bitcoin", "btc", "100k"],
          "yesPrice": 0.67,
          "noPrice": 0.33,
          "volume24h": 250000,
          "url": "https://polymarket.com/event/example",
          "category": "crypto",
          "lastUpdated": "2026-03-31T12:00:00.000Z",
          "numericId": "12345",
          "oneDayPriceChange": 0.04,
          "endDate": "2026-06-30"
        },
        "confidence": 0.85,
        "matchedKeywords": ["bitcoin", "100k"]
      }
    ],
    "matchCount": 1,
    "timestamp": "2026-03-31T12:00:00.000Z",
    "suggested_action": {
      "direction": "YES",
      "confidence": 0.75,
      "edge": 0.12,
      "reasoning": "Bullish sentiment suggests YES is underpriced."
    },
    "sentiment": {
      "sentiment": "bullish",
      "confidence": 0.85,
      "reasoning": "Positive price-action language.",
      "keyPoints": ["Breakout language", "Strong positive framing"]
    },
    "arbitrage": null,
    "metadata": {
      "processing_time_ms": 124,
      "sources_checked": 2,
      "markets_analyzed": 1234,
      "model_version": "v2.0.0"
    }
  }
}
```

#### Response Fields

| Field | Type | Description |
| --- | --- | --- |
| `event_id` | `string` | Deterministic ID derived from the input text. |
| `signal_type` | `arbitrage \| news_event \| sentiment_shift \| user_interest` | High-level signal classification. |
| `urgency` | `low \| medium \| high \| critical` | Estimated urgency for acting on the signal. |
| `data.markets` | `MarketMatch[]` | Ranked matched markets. |
| `data.matchCount` | `number` | Number of returned matches. |
| `data.suggested_action` | `SuggestedAction` | Trading suggestion for the top signal. |
| `data.sentiment` | `SentimentResult` | Sentiment analysis of the input text. |
| `data.arbitrage` | `ArbitrageOpportunity \| null` | Matching arbitrage opportunity if one was found for the top market. |
| `data.metadata` | `object` | Processing diagnostics. |

#### Common Errors

`400 Bad Request`

```json
{
  "event_id": "evt_error",
  "signal_type": "user_interest",
  "urgency": "low",
  "success": false,
  "error": "Missing or invalid \"text\" field in request body."
}
```

Possible validation errors:
- `Request body must be a JSON object.`
- `Missing or invalid "text" field in request body.`
- `Text exceeds 10,000 character limit.`
- `minConfidence must be between 0 and 1.`
- `maxResults must be between 1 and 100.`
- `Malformed JSON request body.`

`503 Service Unavailable`

```json
{
  "event_id": "evt_error",
  "signal_type": "user_interest",
  "urgency": "low",
  "success": false,
  "error": "No markets available. Service temporarily unavailable."
}
```

### 2. `GET /api/markets/arbitrage`

Returns cross-platform arbitrage opportunities between Polymarket and Kalshi.

#### Query Parameters

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `minSpread` | `number` | No | `0.03` | Minimum absolute price spread, from `0` to `1`. |
| `minConfidence` | `number` | No | `0.5` | Minimum cross-platform matching confidence, from `0` to `1`. |
| `limit` | `number` | No | `20` | Maximum number of results. Range: `1` to `100`. |
| `category` | `string` | No | - | Optional category filter. |

#### Example

```bash
curl "https://musashi-api.vercel.app/api/markets/arbitrage?minSpread=0.05&minConfidence=0.6&limit=10&category=crypto"
```

#### Success Response

```json
{
  "success": true,
  "data": {
    "opportunities": [
      {
        "polymarket": {
          "id": "poly-1",
          "platform": "polymarket",
          "title": "Will Bitcoin reach $100k by June 2026?",
          "description": "Example description",
          "keywords": ["bitcoin", "btc", "100k"],
          "yesPrice": 0.63,
          "noPrice": 0.37,
          "volume24h": 450000,
          "url": "https://polymarket.com/event/example",
          "category": "crypto",
          "lastUpdated": "2026-03-31T12:00:00.000Z"
        },
        "kalshi": {
          "id": "kalshi-1",
          "platform": "kalshi",
          "title": "Bitcoin $100k by June 2026",
          "description": "Example description",
          "keywords": ["bitcoin", "btc", "100k"],
          "yesPrice": 0.70,
          "noPrice": 0.30,
          "volume24h": 200000,
          "url": "https://kalshi.com/markets/example",
          "category": "crypto",
          "lastUpdated": "2026-03-31T12:00:00.000Z"
        },
        "spread": 0.07,
        "profitPotential": 0.07,
        "direction": "buy_poly_sell_kalshi",
        "confidence": 0.85,
        "matchReason": "High title similarity"
      }
    ],
    "count": 1,
    "timestamp": "2026-03-31T12:00:00.000Z",
    "filters": {
      "minSpread": 0.05,
      "minConfidence": 0.6,
      "limit": 10,
      "category": "crypto"
    },
    "metadata": {
      "processing_time_ms": 89,
      "markets_analyzed": 1234,
      "polymarket_count": 734,
      "kalshi_count": 500
    }
  }
}
```

#### Direction Values

| Value | Meaning |
| --- | --- |
| `buy_poly_sell_kalshi` | Polymarket YES is cheaper than Kalshi YES. |
| `buy_kalshi_sell_poly` | Kalshi YES is cheaper than Polymarket YES. |

#### Common Errors

`400 Bad Request`

```json
{
  "success": false,
  "error": "Invalid minSpread. Must be between 0 and 1."
}
```

Other validation errors:
- `Invalid minConfidence. Must be between 0 and 1.`
- `Invalid limit. Must be between 1 and 100.`

`503 Service Unavailable`

```json
{
  "success": false,
  "error": "No markets available. Service temporarily unavailable."
}
```

### 3. `GET /api/markets/movers`

Returns markets whose `yesPrice` has moved significantly compared with historical snapshots stored in KV.

#### Query Parameters

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `minChange` | `number` | No | `0.05` | Minimum absolute 1-hour price change, from `0` to `1`. |
| `limit` | `number` | No | `20` | Maximum number of results. Range: `1` to `100`. |
| `category` | `string` | No | - | Optional category filter. |

Note: the SDK currently exposes a `timeframe` option, but the server-side REST endpoint only uses 1-hour price changes today.

#### Example

```bash
curl "https://musashi-api.vercel.app/api/markets/movers?minChange=0.08&limit=20&category=politics"
```

#### Success Response

```json
{
  "success": true,
  "data": {
    "movers": [
      {
        "market": {
          "id": "poly-2",
          "platform": "polymarket",
          "title": "Will candidate X win the election?",
          "description": "Example description",
          "keywords": ["candidate x", "election"],
          "yesPrice": 0.72,
          "noPrice": 0.28,
          "volume24h": 5000000,
          "url": "https://polymarket.com/event/example",
          "category": "politics",
          "lastUpdated": "2026-03-31T12:00:00.000Z"
        },
        "priceChange1h": 0.08,
        "previousPrice": 0.64,
        "currentPrice": 0.72,
        "direction": "up",
        "timestamp": 1774958400000
      }
    ],
    "count": 1,
    "timestamp": "2026-03-31T12:00:00.000Z",
    "filters": {
      "minChange": 0.08,
      "limit": 20,
      "category": "politics"
    },
    "metadata": {
      "processing_time_ms": 45,
      "markets_analyzed": 1234,
      "markets_tracked": 800,
      "storage": "Vercel KV (Redis)",
      "history_retention": "7 days"
    }
  }
}
```

#### Common Errors

`400 Bad Request`

```json
{
  "success": false,
  "error": "Invalid minChange. Must be between 0 and 1."
}
```

Other validation errors:
- `Invalid limit. Must be between 1 and 100.`

`500 Internal Server Error`

```json
{
  "success": false,
  "error": "Vercel KV error message here",
  "note": "Vercel KV storage error. Ensure KV_REST_API_URL and KV_REST_API_TOKEN are set in Vercel environment variables."
}
```

### 4. `GET /api/feed`

Returns the latest analyzed tweets stored in Musashi's feed pipeline.

This endpoint is useful when you want:
- a polling feed for bots
- fresh market-relevant tweets
- category- or urgency-filtered monitoring

#### Query Parameters

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `limit` | `number` | No | `20` | Maximum number of items. Range: `1` to `100`. |
| `category` | `string` | No | - | Category filter. |
| `minUrgency` | `string` | No | - | Minimum urgency: `low`, `medium`, `high`, `critical`. |
| `since` | `string` | No | - | Only include tweets created after this ISO timestamp. |
| `cursor` | `string` | No | - | Tweet ID cursor for pagination. Returns items after this ID. |

#### Supported Categories

- `politics`
- `economics`
- `crypto`
- `technology`
- `geopolitics`
- `sports`
- `breaking_news`
- `finance`

#### Example

```bash
curl "https://musashi-api.vercel.app/api/feed?limit=20&category=crypto&minUrgency=high&since=2026-03-31T00:00:00.000Z"
```

#### Success Response

```json
{
  "success": true,
  "data": {
    "tweets": [
      {
        "tweet": {
          "id": "1900000000000000000",
          "text": "Bitcoin just broke another major level.",
          "author": "example_account",
          "created_at": "2026-03-31T11:58:00.000Z",
          "metrics": {
            "likes": 1200,
            "retweets": 180,
            "replies": 75,
            "quotes": 22
          },
          "url": "https://twitter.com/example_account/status/1900000000000000000"
        },
        "matches": [
          {
            "market": {
              "id": "poly-1",
              "platform": "polymarket",
              "title": "Will Bitcoin reach $100k by June 2026?",
              "description": "Example description",
              "keywords": ["bitcoin", "btc", "100k"],
              "yesPrice": 0.67,
              "noPrice": 0.33,
              "volume24h": 250000,
              "url": "https://polymarket.com/event/example",
              "category": "crypto",
              "lastUpdated": "2026-03-31T12:00:00.000Z"
            },
            "confidence": 0.82,
            "matchedKeywords": ["bitcoin"]
          }
        ],
        "sentiment": {
          "sentiment": "bullish",
          "confidence": 0.81,
          "reasoning": "Positive tone",
          "keyPoints": ["Momentum language"]
        },
        "suggested_action": {
          "direction": "YES",
          "confidence": 0.68,
          "edge": 0.11,
          "reasoning": "Bullish sentiment suggests upside."
        },
        "category": "crypto",
        "urgency": "high",
        "confidence": 0.82,
        "analyzed_at": "2026-03-31T11:58:10.000Z",
        "collected_at": "2026-03-31T11:58:15.000Z"
      }
    ],
    "count": 1,
    "timestamp": "2026-03-31T12:00:00.000Z",
    "cursor": "1900000000000000000",
    "filters": {
      "limit": 20,
      "category": "crypto",
      "minUrgency": "high",
      "since": "2026-03-31T00:00:00.000Z"
    },
    "metadata": {
      "processing_time_ms": 33,
      "total_in_kv": 1500,
      "cached": false
    }
  }
}
```

#### Empty Response

An empty feed is still a successful request:

```json
{
  "success": true,
  "data": {
    "tweets": [],
    "count": 0,
    "timestamp": "2026-03-31T12:00:00.000Z",
    "filters": {
      "limit": 20
    },
    "metadata": {
      "processing_time_ms": 5,
      "total_in_kv": 0
    }
  }
}
```

#### Common Errors

`400 Bad Request`

```json
{
  "success": false,
  "error": "Invalid minUrgency. Must be one of: low, medium, high, critical"
}
```

Other validation errors:
- `Limit must be between 1 and 100.`
- `Invalid category. Must be one of: politics, economics, crypto, technology, geopolitics, sports, breaking_news, finance`
- `Invalid "since" timestamp. Use ISO 8601 format.`

`503 Service Unavailable`

```json
{
  "success": false,
  "error": "Feed service temporarily unavailable. Check KV configuration and try again.",
  "note": "Ensure the local KV REST URL and credential are configured for feed endpoints."
}
```

### 5. `GET /api/feed/stats`

Returns aggregated statistics for the tweet feed.

#### Example

```bash
curl "https://musashi-api.vercel.app/api/feed/stats"
```

#### Success Response

```json
{
  "success": true,
  "data": {
    "timestamp": "2026-03-31T12:00:00.000Z",
    "last_collection": "2026-03-31T11:58:15.000Z",
    "tweets": {
      "last_1h": 24,
      "last_6h": 118,
      "last_24h": 402
    },
    "by_category": {
      "politics": 82,
      "economics": 40,
      "crypto": 110,
      "technology": 55,
      "geopolitics": 32,
      "sports": 47,
      "breaking_news": 20,
      "finance": 16
    },
    "by_urgency": {
      "low": 220,
      "medium": 120,
      "high": 50,
      "critical": 12
    },
    "top_markets": [
      {
        "market": {
          "id": "poly-1",
          "platform": "polymarket",
          "title": "Will Bitcoin reach $100k by June 2026?",
          "description": "Example description",
          "keywords": ["bitcoin", "btc", "100k"],
          "yesPrice": 0.67,
          "noPrice": 0.33,
          "volume24h": 250000,
          "url": "https://polymarket.com/event/example",
          "category": "crypto",
          "lastUpdated": "2026-03-31T12:00:00.000Z"
        },
        "mention_count": 19
      }
    ],
    "metadata": {
      "processing_time_ms": 18
    }
  }
}
```

#### Common Errors

`503 Service Unavailable`

```json
{
  "success": false,
  "error": "Feed stats temporarily unavailable. Check KV configuration and try again.",
  "note": "Ensure the local KV REST URL and credential are configured for feed stats."
}
```

Quota fallback errors may also return:

```json
{
  "success": false,
  "error": "Service temporarily unavailable due to quota limits. No cached data available."
}
```

### 6. `GET /api/feed/accounts`

Returns the curated list of Twitter/X accounts monitored by the feed pipeline.

#### Example

```bash
curl "https://musashi-api.vercel.app/api/feed/accounts"
```

#### Success Response

```json
{
  "success": true,
  "data": {
    "accounts": [
      {
        "username": "example_account",
        "category": "crypto",
        "priority": "high",
        "description": "High-signal market commentary"
      }
    ],
    "count": 1,
    "by_category": {
      "crypto": 1
    },
    "by_priority": {
      "high": 1,
      "medium": 0
    },
    "metadata": {
      "processing_time_ms": 2
    }
  }
}
```

#### Common Errors

`500 Internal Server Error`

```json
{
  "success": false,
  "error": "Unknown error"
}
```

### 7. `GET /api/health`

Returns API health information and dependency status.

Important: this endpoint may return HTTP `503` when the system is degraded or down, even if the JSON body includes structured health data.

#### Example

```bash
curl "https://musashi-api.vercel.app/api/health"
```

#### Success Response

```json
{
  "success": true,
  "data": {
    "status": "healthy",
    "timestamp": "2026-03-31T12:00:00.000Z",
    "uptime_ms": 123456,
    "response_time_ms": 45,
    "version": "2.0.0",
    "services": {
      "polymarket": {
        "status": "healthy",
        "markets": 734
      },
      "kalshi": {
        "status": "healthy",
        "markets": 500
      }
    },
    "endpoints": {
      "/api/analyze-text": {
        "method": "POST",
        "description": "Analyze text and return matching markets with trading signals",
        "status": "healthy"
      },
      "/api/markets/arbitrage": {
        "method": "GET",
        "description": "Get cross-platform arbitrage opportunities",
        "status": "healthy"
      },
      "/api/markets/movers": {
        "method": "GET",
        "description": "Get markets with significant price changes",
        "status": "healthy"
      },
      "/api/health": {
        "method": "GET",
        "description": "API health check",
        "status": "healthy"
      }
    },
    "limits": {
      "max_markets_per_request": 5,
      "cache_ttl_seconds": 300,
      "rate_limit": "none (currently)"
    }
  }
}
```

#### Status Values

| Value | Meaning |
| --- | --- |
| `healthy` | Polymarket and Kalshi checks both succeeded. |
| `degraded` | One upstream service failed. |
| `down` | Both upstream services failed. |

#### Error Response

```json
{
  "success": false,
  "error": "Internal server error"
}
```

## Shared Output Schemas

### `Market`

```json
{
  "id": "string",
  "platform": "polymarket",
  "title": "string",
  "description": "string",
  "keywords": ["string"],
  "yesPrice": 0.67,
  "noPrice": 0.33,
  "volume24h": 250000,
  "url": "https://example.com",
  "category": "crypto",
  "lastUpdated": "2026-03-31T12:00:00.000Z",
  "numericId": "12345",
  "oneDayPriceChange": 0.04,
  "endDate": "2026-06-30"
}
```

### `MarketMatch`

```json
{
  "market": {},
  "confidence": 0.85,
  "matchedKeywords": ["bitcoin", "100k"]
}
```

### `SuggestedAction`

```json
{
  "direction": "YES",
  "confidence": 0.75,
  "edge": 0.12,
  "reasoning": "Bullish sentiment suggests YES is underpriced."
}
```

`direction` can be:
- `YES`
- `NO`
- `HOLD`

### `SentimentResult`

```json
{
  "sentiment": "bullish",
  "confidence": 0.85,
  "reasoning": "Positive language and momentum.",
  "keyPoints": ["Breakout framing", "Strong conviction wording"]
}
```

`sentiment` can be:
- `bullish`
- `bearish`
- `neutral`

### `ArbitrageOpportunity`

```json
{
  "polymarket": {},
  "kalshi": {},
  "spread": 0.07,
  "profitPotential": 0.07,
  "direction": "buy_poly_sell_kalshi",
  "confidence": 0.85,
  "matchReason": "High title similarity"
}
```

### `AnalyzedTweet`

```json
{
  "tweet": {
    "id": "1900000000000000000",
    "text": "string",
    "author": "username",
    "created_at": "2026-03-31T11:58:00.000Z",
    "metrics": {
      "likes": 1200,
      "retweets": 180,
      "replies": 75,
      "quotes": 22
    },
    "url": "https://twitter.com/username/status/1900000000000000000"
  },
  "matches": [],
  "sentiment": {},
  "suggested_action": {},
  "category": "crypto",
  "urgency": "high",
  "confidence": 0.82,
  "analyzed_at": "2026-03-31T11:58:10.000Z",
  "collected_at": "2026-03-31T11:58:15.000Z"
}
```

## JavaScript Example

```js
const baseUrl = "https://musashi-api.vercel.app";

async function main() {
  const response = await fetch(`${baseUrl}/api/analyze-text`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      text: "The Fed just signaled a possible rate cut.",
      minConfidence: 0.4,
      maxResults: 3
    })
  });

  const json = await response.json();
  console.log(json);
}

main().catch(console.error);
```

## Python Example

```python
import requests

base_url = "https://musashi-api.vercel.app"

response = requests.post(
    f"{base_url}/api/analyze-text",
    json={
        "text": "The Fed just signaled a possible rate cut.",
        "minConfidence": 0.4,
        "maxResults": 3,
    },
    timeout=30,
)

print(response.status_code)
print(response.json())
```

## cURL Examples for All Endpoints

```bash
curl -X POST https://musashi-api.vercel.app/api/analyze-text \
  -H "Content-Type: application/json" \
  -d '{"text":"Bitcoin just hit a new high","minConfidence":0.3,"maxResults":5}'

curl "https://musashi-api.vercel.app/api/markets/arbitrage?minSpread=0.03&minConfidence=0.5&limit=20"

curl "https://musashi-api.vercel.app/api/markets/movers?minChange=0.05&limit=20"

curl "https://musashi-api.vercel.app/api/feed?limit=20"

curl "https://musashi-api.vercel.app/api/feed/stats"

curl "https://musashi-api.vercel.app/api/feed/accounts"

curl "https://musashi-api.vercel.app/api/health"
```

## Notes for SDK Users

The TypeScript SDK lives at [src/sdk/musashi-agent.ts](../Musashi/src/sdk/musashi-agent.ts).

Useful methods:
- `analyzeText(text, options)`
- `getArbitrage(options)`
- `getMovers(options)`
- `checkHealth()`
- `getFeed(options)`
- `getFeedStats()`
- `getFeedAccounts()`
- `onSignal(...)`
- `onArbitrage(...)`
- `onMovers(...)`
- `onFeed(...)`

## Non-Public / Separate Backend APIs

This README documents the public prediction-market REST API under `api/`.

The repository also contains a separate local backend server documented in [BACKEND_API.md](../Musashi/BACKEND_API.md). That server is for Supabase-backed application data and is not part of the public Musashi market-intelligence REST surface documented above.
